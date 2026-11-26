from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.parse
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from kintaro_auto.appeals import (
    classify_appeal_folder,
    list_studio_post_ids,
    remove_account_restrictions,
    sort_account_into_folder,
    wait_and_appeal_settle,
)
from kintaro_auto.captions import prepare_description_and_mentions
from kintaro_auto.client import (
    AuthChallengeRequiredError,
    KintaroApiError,
    KintaroClient,
)
from kintaro_auto.config import AppConfig
from kintaro_auto.models import (
    GEO_LOCALE_DEFAULTS,
    AccountRecord,
    CampaignAccountResult,
    CampaignConfig,
    PostHealthReport,
    UploadedPostRecord,
    UploadTask,
    allocate_sticky_proxy,
    apply_device_geo_profile,
    utc_now_iso,
)
from kintaro_auto.response_adapters import (
    extract_author_username,
    extract_aweme_id,
    extract_feed_items,
    extract_first_music_id,
    extract_play_count,
)
from kintaro_auto.storage import StateStore
from kintaro_auto.uniquifier import PreparedMedia, uniquify_photos, uniquify_video

logger = logging.getLogger("kintaro_auto")


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    return datetime.fromisoformat(ts)


class AutomationPipeline:
    def __init__(
        self,
        config: AppConfig,
        store: StateStore,
        client: KintaroClient,
        *,
        sleep_fn: Callable[[float], asyncio.Future[None]] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client
        self._sleep = sleep_fn or asyncio.sleep
        self._rng = rng or random.Random()

    async def _human_pause(self, low: float, high: float) -> None:
        if high <= 0:
            return
        delay = self._rng.uniform(max(0.0, low), max(low, high))
        if delay > 0:
            await self._sleep(delay)

    # ── 1. Device & Session Lifecycle ─────────────────────────────────────────

    @staticmethod
    def _extract_redirect_cluster(payload: object, current_region: str = "") -> str | None:
        if not isinstance(payload, dict):
            return None
        raw = urllib.parse.unquote(json.dumps(payload, ensure_ascii=False)).lower()
        cur = current_region.upper()
        eu_regions = {"DE", "GB", "FR", "IT", "ES", "PL", "NL"}

        if "tiktokv.eu" in raw or "no1a" in raw or "useastred" in raw or "euw" in raw:
            return None if cur in eu_regions else "DE"
        if "tiktokv.us" in raw or "useast5" in raw or "useast8" in raw:
            return None if cur == "US" else "US"
        if "alisg" in raw:
            return None if cur in {"VN", "SG", "MY", "TH", "ID", "PH", "JP", "KR"} else "SG"

        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(data, dict):
            for key in ("redirect_region", "store_region", "region", "alpha2", "country_code"):
                val = data.get(key)
                if isinstance(val, str) and len(val.strip()) == 2 and val.strip().isalpha():
                    cc = val.strip().upper()
                    return None if cc == cur else cc
        return None

    async def _register_and_boot_device(
        self, account: AccountRecord, region_code: str
    ) -> None:
        proxy = account.proxy_for_country(region_code)
        logger.info(
            "[%s] Регистрация устройства (region=%s)...",
            account.account_id,
            region_code,
        )
        await self.client.register_device(
            account, region_override=region_code, proxy_override=proxy
        )
        self.store.save_account(account)

    async def ensure_authenticated(
        self,
        account: AccountRecord,
        code_provider: Callable[[AccountRecord], str] | None = None,
    ) -> AccountRecord:
        now = datetime.now(timezone.utc)
        if not account.authenticated and account.login_cooldown_until:
            until_dt = _parse_iso(account.login_cooldown_until)
            if until_dt and now < until_dt:
                rem_min = (until_dt - now).total_seconds() / 60.0
                raise RuntimeError(
                    f"[{account.account_id}] Аккаунт на антибрут-кулдауне TikTok (error_code 7/1206). "
                    f"Подожди ещё {rem_min:.0f} мин, чтобы не продлевать блокировку и не тратить $0.05."
                )

        # Ensure sticky residential IP & clean device state before an unauthenticated login handshake
        if not account.authenticated:
            account.proxy = allocate_sticky_proxy(account.proxy, preserve_existing_session=True)
            if account.device is not None and not account.pending_verify and account.auth_method != "device_only":
                account.device = None
                self.store.save_account(account)

        login_region = (
            account.auth_region
            or ("DE" if (account.totp_token or account.use_outlook_hub) else account.region)
        ).upper()

        # Step A & Mailbox Prep (run Outlook Hub / IMAP prep in parallel with /v1/device/register!)
        pre_outlook_token = ""
        pre_imap_server: tuple[str, int] | None = None
        known_old_ids: set[str] | None = None

        need_outlook = (
            not account.authenticated
            and account.auth_method != "device_only"
            and account.use_outlook_hub
            and bool(account.email and account.mail_password)
        )
        need_imap = (
            not account.authenticated
            and account.auth_method == "email"
            and not account.use_outlook_hub
            and bool(account.email and account.mail_password)
        )

        if account.device is None and (need_outlook or need_imap):
            if need_outlook:
                from kintaro_auto.outlook_hub import prepare_outlook_hub_mailbox

                logger.info(
                    "[%s] ⚡ Параллельный запуск: Outlook Hub (%s) + /v1/device/register (region=%s)...",
                    account.account_id,
                    account.email,
                    login_region,
                )
                mail_coro = asyncio.to_thread(
                    prepare_outlook_hub_mailbox,
                    outlook_email=account.email,
                    outlook_login=account.mail_login or account.email,
                    outlook_password=account.mail_password,
                    recovery_email=account.recovery_email,
                    recovery_password=account.recovery_password,
                    hotmail_totp=account.mail_totp,
                    log_fn=lambda m: logger.info("[%s] %s", account.account_id, m),
                )
                (pre_outlook_token, known_old_ids), _ = await asyncio.gather(
                    mail_coro, self._register_and_boot_device(account, login_region)
                )
            else:
                from kintaro_auto.mail_imap import prepare_imap_mailbox

                logger.info(
                    "[%s] ⚡ Параллельный запуск: IMAP (%s) + /v1/device/register (region=%s)...",
                    account.account_id,
                    account.email,
                    login_region,
                )
                imap_coro = asyncio.to_thread(
                    prepare_imap_mailbox,
                    account.email,
                    account.mail_password,
                    custom_imap_host=account.imap_host or None,
                    log_fn=lambda m: logger.info("[%s] %s", account.account_id, m),
                )
                (pre_imap_server, known_old_ids), _ = await asyncio.gather(
                    imap_coro, self._register_and_boot_device(account, login_region)
                )
        else:
            if account.device is None:
                await self._register_and_boot_device(account, login_region)
            if need_outlook:
                from kintaro_auto.outlook_hub import prepare_outlook_hub_mailbox

                logger.info(
                    "[%s] Подключение почты Outlook (%s) через Microsoft Graph API перед входом в TikTok...",
                    account.account_id,
                    account.email,
                )
                pre_outlook_token, known_old_ids = await asyncio.to_thread(
                    prepare_outlook_hub_mailbox,
                    outlook_email=account.email,
                    outlook_login=account.mail_login or account.email,
                    outlook_password=account.mail_password,
                    recovery_email=account.recovery_email,
                    recovery_password=account.recovery_password,
                    hotmail_totp=account.mail_totp,
                    log_fn=lambda m: logger.info("[%s] %s", account.account_id, m),
                )
            elif need_imap:
                from kintaro_auto.mail_imap import prepare_imap_mailbox

                pre_imap_server, known_old_ids = await asyncio.to_thread(
                    prepare_imap_mailbox,
                    account.email,
                    account.mail_password,
                    custom_imap_host=account.imap_host or None,
                    log_fn=lambda m: logger.info("[%s] %s", account.account_id, m),
                )

        # Step B: Sign in if not yet authenticated
        if not account.authenticated:
            if account.auth_method == "device_only":
                await self.client.get_self(account)
                account.authenticated = True
                account.first_authenticated_at = account.first_authenticated_at or utc_now_iso()
                self.store.save_account(account)
                return account

            for cluster_attempt in range(2):
                login_region = (
                    account.auth_region
                    or ("DE" if (account.totp_token or account.use_outlook_hub) else account.region)
                ).upper()
                login_proxy = account.proxy_for_country(login_region)
                signin_started_ts = time.time()
                logger.info(
                    "[%s] Вход методом `%s`%s (домашний кластер=%s)...",
                    account.account_id,
                    account.auth_method,
                    " + TOTP" if account.totp_token else (" + OutlookHub" if account.use_outlook_hub else ""),
                    login_region,
                )
                try:
                    if account.auth_method == "username":
                        await self.client.signin_username(
                            account, region_override=login_region, proxy_override=login_proxy
                        )
                    elif account.auth_method == "email":
                        await self.client.signin_email(
                            account, region_override=login_region, proxy_override=login_proxy
                        )
                    elif account.auth_method == "mobile":
                        await self.client.signin_mobile(account)
                    else:
                        raise ValueError(f"Неизвестный auth_method: {account.auth_method}")
                    break
                except AuthChallengeRequiredError as exc:
                    # If 403 suspicious_login happened due to IDC mismatch and we have TOTP,
                    # re-register under the detected home IDC so TOTP solves 2FA directly without email!
                    redirect_cc = self._extract_redirect_cluster(exc.payload, login_region)
                    if (
                        cluster_attempt == 0
                        and (account.totp_token or account.use_outlook_hub)
                        and redirect_cc
                        and redirect_cc != login_region
                    ):
                        logger.warning(
                            "[%s] 403 показал домашний кластер `%s` (вместо `%s`). "
                            "Перерегистрация устройства под родной кластер `%s` против плашки смены региона...",
                            account.account_id,
                            redirect_cc,
                            login_region,
                            redirect_cc,
                        )
                        account.auth_region = redirect_cc
                        account.pending_verify = None
                        account.device = None
                        await self._register_and_boot_device(account, redirect_cc)
                        continue

                    self.store.save_account(account)
                    if account.use_outlook_hub and account.email and account.mail_password:
                        from kintaro_auto.outlook_hub import wait_for_outlook_hub_code

                        logger.info(
                            "[%s] TikTok запросил 6-значный код (HTTP 403) — читаю новый OTP из %s...",
                            account.account_id,
                            account.email,
                        )
                        otp_code = await asyncio.to_thread(
                            wait_for_outlook_hub_code,
                            outlook_email=account.email,
                            outlook_login=account.mail_login or account.email,
                            outlook_password=account.mail_password,
                            recovery_email=account.recovery_email,
                            recovery_password=account.recovery_password,
                            hotmail_totp=account.mail_totp,
                            min_timestamp=signin_started_ts,
                            pre_access_token=pre_outlook_token,
                            known_old_ids=known_old_ids,
                            timeout_sec=120,
                            poll_interval_sec=1.0,
                            log_fn=lambda m: logger.info("[%s] %s", account.account_id, m),
                        )
                        if not otp_code:
                            raise RuntimeError(
                                f"[{account.account_id}] Таймаут ожидания нового 6-значного кода TikTok в Outlook ({account.email})."
                            )
                        try:
                            return await self.confirm_signin_code(account, otp_code, proxy_override=login_proxy)
                        except KintaroApiError as c_exc:
                            c_data = c_exc.payload.get("data") if isinstance(c_exc.payload, dict) and isinstance(c_exc.payload.get("data"), dict) else {}
                            c_err = c_data.get("error_code")
                            if cluster_attempt == 0 and (c_err == 1011 or "ri_report" in str(c_exc)):
                                fallback_cc = account.region if login_region != account.region else "US"
                                logger.warning(
                                    "[%s] send_code вернул %s на кластере %s — перерегистрирую устройство под %s и повторяю вход...",
                                    account.account_id,
                                    c_err or "ri_report",
                                    login_region,
                                    fallback_cc,
                                )
                                account.auth_region = fallback_cc
                                account.pending_verify = None
                                account.device = None
                                account.proxy = allocate_sticky_proxy(account.proxy, preserve_existing_session=False)
                                await self._register_and_boot_device(account, fallback_cc)
                                continue
                            raise

                    if need_imap and account.email and account.mail_password:
                        from kintaro_auto.mail_imap import wait_for_imap_code

                        logger.info(
                            "[%s] TikTok запросил 6-значный код (HTTP 403) — читаю новый OTP по IMAP (%s)...",
                            account.account_id,
                            account.email,
                        )
                        otp_code = await asyncio.to_thread(
                            wait_for_imap_code,
                            account.email,
                            account.mail_password,
                            since_timestamp=signin_started_ts,
                            pre_server=pre_imap_server,
                            known_old_uids=known_old_ids,
                            custom_imap_host=account.imap_host or None,
                            timeout_sec=120,
                            poll_interval_sec=2.5,
                            log_fn=lambda m: logger.info("[%s] %s", account.account_id, m),
                        )
                        if not otp_code:
                            raise RuntimeError(
                                f"[{account.account_id}] Таймаут ожидания нового 6-значного кода TikTok в Outlook ({account.email})."
                            )
                        try:
                            return await self.confirm_signin_code(account, otp_code, proxy_override=login_proxy)
                        except KintaroApiError as c_exc:
                            c_data = c_exc.payload.get("data") if isinstance(c_exc.payload, dict) and isinstance(c_exc.payload.get("data"), dict) else {}
                            c_err = c_data.get("error_code")
                            if cluster_attempt == 0 and (c_err == 1011 or "ri_report" in str(c_exc)):
                                fallback_cc = account.region if login_region != account.region else "US"
                                logger.warning(
                                    "[%s] send_code вернул %s на кластере %s — перерегистрирую устройство под %s и повторяю вход...",
                                    account.account_id,
                                    c_err or "ri_report",
                                    login_region,
                                    fallback_cc,
                                )
                                account.auth_region = fallback_cc
                                account.pending_verify = None
                                account.device = None
                                account.proxy = allocate_sticky_proxy(account.proxy, preserve_existing_session=False)
                                await self._register_and_boot_device(account, fallback_cc)
                                continue
                            raise

                    if code_provider is None:
                        raise
                    code = code_provider(account)
                    return await self.confirm_signin_code(account, code, proxy_override=login_proxy)
                except KintaroApiError as exc:
                    data = (
                        exc.payload.get("data")
                        if isinstance(exc.payload, dict) and isinstance(exc.payload.get("data"), dict)
                        else {}
                    )
                    err_code = data.get("error_code")
                    if err_code in (7, 1206):
                        account.login_cooldown_until = (
                            datetime.now(timezone.utc) + timedelta(minutes=60)
                        ).isoformat()
                        self.store.save_account(account)
                        raise RuntimeError(
                            f"[{account.account_id}] TikTok вернул антибрут-лок (error_code={err_code}: {data.get('description')}). "
                            f"Поставлен защитный кулдаун 60 мин."
                        ) from exc

                    redirect_cc = self._extract_redirect_cluster(exc.payload, login_region)
                    if (
                        cluster_attempt == 0
                        and redirect_cc
                        and redirect_cc != login_region
                    ):
                        account.auth_region = redirect_cc
                        account.device = None
                        await self._register_and_boot_device(account, redirect_cc)
                        continue
                    raise

            account.authenticated = True
            account.login_cooldown_until = None
            account.pending_verify = None
            account.first_authenticated_at = account.first_authenticated_at or utc_now_iso()

            # Switch SIM / Locale / Timezone profile inside x-device to target posting region (e.g. DE -> US)
            # WITHOUT calling update_hosts (which would break EU IDC routing!)
            if account.device is not None and login_region != account.region:
                logger.info(
                    "[%s] Применение SIM/гео профиля `%s` поверх сессии `%s`...",
                    account.account_id,
                    account.region,
                    login_region,
                )
                account.device = apply_device_geo_profile(account.device, account.region)

            account.proxy = account.proxy_for_country(account.region)
            await self._human_pause(0.8, 1.8)
            try:
                await self.client.get_self(account)
            except KintaroApiError as exc:
                logger.warning("[%s] Предупреждение /v1/account/self: %s", account.account_id, exc)
            account.last_hosts_update_at = utc_now_iso()
            account.last_token_beat_at = utc_now_iso()
            self.store.save_account(account)
            return account

        return account

    async def confirm_signin_code(
        self,
        account: AccountRecord,
        code: str,
        *,
        proxy_override: str | None = None,
    ) -> AccountRecord:
        if account.auth_method == "username":
            await self.client.signin_username_send_code(account, code, proxy_override=proxy_override)
        elif account.auth_method == "email":
            try:
                await self.client.signin_email_send_code(account, code, proxy_override=proxy_override)
            except KintaroApiError:
                if account.username and account.password and account.pending_verify:
                    logger.info(
                        "[%s] Повтор отправки кода через /v1/auth/signin/username/send_code...",
                        account.account_id,
                    )
                    await self.client.signin_username_send_code(account, code, proxy_override=proxy_override)
                else:
                    raise
        elif account.auth_method == "mobile":
            await self.client.signin_mobile_send_code(account, code)
        else:
            raise ValueError(
                f"[{account.account_id}] Подтверждение кода не применимо для `{account.auth_method}`."
            )

        account.authenticated = True
        account.login_cooldown_until = None
        account.pending_verify = None
        account.first_authenticated_at = account.first_authenticated_at or utc_now_iso()
        login_reg = (account.auth_region or account.region).upper()
        if account.device is not None and login_reg != account.region:
            account.device = apply_device_geo_profile(account.device, account.region)
        account.last_hosts_update_at = utc_now_iso()
        self.store.save_account(account)

        await self._human_pause(1.0, 2.0)
        try:
            await self.client.token_beat(account)
        except KintaroApiError as exc:
            logger.warning("[%s] Предупреждение token_beat: %s", account.account_id, exc)
        account.last_token_beat_at = utc_now_iso()
        self.store.save_account(account)
        return account

    # ── 2. Anti-Shadowban Rate & Cooldown Guards ──────────────────────────────

    def check_posting_eligibility(
        self, account: AccountRecord, *, force: bool = False
    ) -> tuple[bool, str]:
        if force:
            return True, "forced"

        now = datetime.now(timezone.utc)
        w = self.config.warmup

        # 1. Fresh session aging before first upload
        first_auth = _parse_iso(account.first_authenticated_at)
        if first_auth is not None and w.min_session_age_minutes_before_first_post > 0:
            age_min = (now - first_auth).total_seconds() / 60.0
            if age_min < w.min_session_age_minutes_before_first_post:
                rem = w.min_session_age_minutes_before_first_post - age_min
                return (
                    False,
                    f"Свежая сессия ({age_min:.1f} мин). До безопасного первого залива осталось {rem:.1f} мин.",
                )

        # 2. Max posts per 24h
        day_ago = now - timedelta(hours=24)
        recent_day = [
            p
            for p in account.upload_history
            if (_parse_iso(p.uploaded_at) or now) >= day_ago
        ]
        if len(recent_day) >= w.max_posts_per_account_per_day:
            return (
                False,
                f"Достигнут суточный лимит ({len(recent_day)}/{w.max_posts_per_account_per_day} за 24ч).",
            )

        # 3. Minimum cooldown between consecutive posts on the same account
        if account.upload_history and w.min_hours_between_posts_per_account > 0:
            last_ts = max(
                (_parse_iso(p.uploaded_at) for p in account.upload_history if _parse_iso(p.uploaded_at)),
                default=None,
            )
            if last_ts is not None:
                hours_since = (now - last_ts).total_seconds() / 3600.0
                if hours_since < w.min_hours_between_posts_per_account:
                    rem_h = w.min_hours_between_posts_per_account - hours_since
                    return (
                        False,
                        f"Кулдаун между постами ({hours_since:.2f}ч из {w.min_hours_between_posts_per_account:.2f}ч, осталось {rem_h * 60:.0f} мин).",
                    )

        return True, "ok"

    def select_account_for_task(
        self, task: UploadTask, *, force: bool = False
    ) -> AccountRecord:
        if task.account_id:
            acc = self.store.load_account(task.account_id)
            ok, reason = self.check_posting_eligibility(acc, force=force)
            if not ok:
                raise RuntimeError(f"[{acc.account_id}] Залив отложен защитой от теней: {reason}")
            return acc

        candidates: list[tuple[int, float, AccountRecord]] = []
        now = datetime.now(timezone.utc)
        day_ago = now - timedelta(hours=24)
        reasons: list[str] = []

        for acc in self.store.list_accounts():
            if not acc.authenticated and acc.auth_method == "device_only" and acc.device is None:
                reasons.append(f"{acc.account_id}: нет устройства")
                continue
            ok, reason = self.check_posting_eligibility(acc, force=force)
            if not ok:
                reasons.append(f"{acc.account_id}: {reason}")
                continue
            posts_24h = sum(
                1
                for p in acc.upload_history
                if (_parse_iso(p.uploaded_at) or now) >= day_ago
            )
            last_upload = max(
                (
                    (_parse_iso(p.uploaded_at) or datetime.min.replace(tzinfo=timezone.utc)).timestamp()
                    for p in acc.upload_history
                ),
                default=0.0,
            )
            candidates.append((posts_24h, last_upload, acc))

        if not candidates:
            detail = "; ".join(reasons) if reasons else "нет импортированных аккаунтов"
            raise RuntimeError(f"Нет доступных аккаунтов для безопасного залива ({detail}).")

        # Prefer account with fewest posts in last 24h, then longest idle since last post
        candidates.sort(key=lambda t: (t[0], t[1]))
        return candidates[0][2]

    # ── 3. Human-Like Feed Warm-Up ────────────────────────────────────────────

    async def run_warmup(self, account: AccountRecord) -> dict[str, int]:
        await self.ensure_authenticated(account)
        w = self.config.warmup
        stats = {"rounds": 0, "views": 0, "likes": 0, "comments_read": 0, "follows": 0}

        await self.client.get_self(account)
        await self._human_pause(w.action_delay_seconds_min, w.action_delay_seconds_max)

        rounds = self._rng.randint(w.feed_rounds_min, max(w.feed_rounds_min, w.feed_rounds_max))
        seen_ids: set[str] = set()

        for r_idx in range(rounds):
            pull_type = "0" if r_idx == 0 else "2"
            try:
                feed_resp = await self.client.get_recommendations(account, pull_type=pull_type)
            except KintaroApiError as exc:
                # Kintaro US (`tiktokv.us`) backend currently has a server-side zstd bug (`Unknown frame descriptor`)
                # on `/v1/feed/recommendations`. Fall back to music chart & inbox discovery without aborting upload.
                logger.warning(
                    "[%s] Лента рекомендаций вернула %s (%s) — переключаюсь на резервный прогрев (music/chart + suggested_users).",
                    account.account_id,
                    exc.status_code,
                    exc.payload,
                )
                try:
                    await self.client.get_music_chart(account, count=15)
                    await self._human_pause(w.action_delay_seconds_min, w.action_delay_seconds_max)
                    await self.client.get_suggested_users(account, count=15)
                except KintaroApiError:
                    pass
                break
            items = extract_feed_items(feed_resp, self.config.response_mapping)
            stats["rounds"] += 1

            target_views = self._rng.randint(
                w.views_per_round_min, max(w.views_per_round_min, w.views_per_round_max)
            )
            viewed_this_round = 0

            for item in items:
                if viewed_this_round >= target_views:
                    break
                aweme_id = extract_aweme_id(item, self.config.response_mapping)
                if not aweme_id or aweme_id in seen_ids:
                    continue
                seen_ids.add(aweme_id)

                playtime_ms = self._rng.randint(
                    w.playtime_ms_min, max(w.playtime_ms_min, w.playtime_ms_max)
                )
                await self.client.view_post(account, aweme_id, playtime_ms)
                stats["views"] += 1
                viewed_this_round += 1

                await self._human_pause(
                    w.action_delay_seconds_min, w.action_delay_seconds_max
                )

                if self._rng.random() < w.read_comments_probability:
                    await self.client.get_post_comments(account, aweme_id, count=8)
                    stats["comments_read"] += 1
                    await self._human_pause(1.0, 3.0)

                if self._rng.random() < w.like_probability:
                    await self.client.like_post(account, aweme_id)
                    stats["likes"] += 1
                    await self._human_pause(0.8, 2.2)

                if self._rng.random() < w.follow_probability:
                    author = extract_author_username(item)
                    if author:
                        await self.client.follow_user(account, author)
                        stats["follows"] += 1
                        await self._human_pause(1.0, 2.5)

        account.last_warmup_at = utc_now_iso()
        self.store.save_account(account)
        return stats

    # ── 4. Full Upload Flow (Warmup -> Uniquify -> Multipart Upload) ──────────

    async def execute_upload_task(
        self,
        task: UploadTask,
        *,
        force_cooldown: bool = False,
        skip_warmup: bool = False,
        delete_old_posts: bool = False,
        appeal_after_upload: bool = False,
        settle_after_upload: bool = True,
        appeal_studio_posts: bool = False,
        hide_rejected: bool = True,
    ) -> tuple[AccountRecord, UploadedPostRecord]:
        account = self.select_account_for_task(task, force=force_cooldown)
        await self.ensure_authenticated(account)

        # Re-check eligibility after potential fresh first-time authentication!
        ok, reason = self.check_posting_eligibility(account, force=force_cooldown)
        if not ok:
            raise RuntimeError(f"[{account.account_id}] Залив остановлен защитой от теней: {reason}")

        if delete_old_posts:
            logger.info("[%s] Удаление старых постов перед заливом...", account.account_id)
            deleted_cnt = await self.delete_all_account_posts(account)
            logger.info("[%s] Удалено старых постов: %d", account.account_id, deleted_cnt)

        # Organic pre-upload warm-up if enabled and not warmed up recently (<20 min)
        if self.config.warmup.enabled and not skip_warmup:
            last_warm = _parse_iso(account.last_warmup_at)
            now = datetime.now(timezone.utc)
            if last_warm is None or (now - last_warm) > timedelta(minutes=20):
                logger.info("[%s] Запуск прогрева ленты перед публикацией...", account.account_id)
                await self.run_warmup(account)

            await self._human_pause(
                self.config.warmup.pre_upload_pause_seconds_min,
                self.config.warmup.pre_upload_pause_seconds_max,
            )

        # Prepare caption, spintax, optional hashtag shuffle, and trailing-space mentions per Kintaro spec
        description, mentions = prepare_description_and_mentions(
            task.description,
            task.mentions,
            rng=self._rng,
            shuffle_tags=task.randomize_tags,
        )

        # Resolve optional music search
        music_id = task.music_id.strip()
        if not music_id and task.music_search_keyword:
            music_resp = await self.client.search_music(
                account,
                keyword=task.music_search_keyword,
                region_override=task.region_override,
            )
            found_music = extract_first_music_id(music_resp)
            if found_music:
                music_id = found_music

        temp_files: list[PreparedMedia] = []
        try:
            if task.media_type == "video":
                if not task.video_path:
                    raise ValueError(f"[{task.task_id}] Не указан `video_path`.")
                src = Path(task.video_path)
                media = uniquify_video(src, self.config.uniquifier, rng=self._rng)
                temp_files.append(media)
                source_list = [str(src)]

                resp = await self.client.create_video_post(
                    account,
                    media,
                    description=description,
                    description_mentions=mentions,
                    music_id=music_id,
                    region_override=task.region_override,
                    is_private=task.is_private,
                    disable_comments=task.disable_comments,
                    disable_duet=task.disable_duet,
                    disable_react=task.disable_react,
                    disable_stitch=task.disable_stitch,
                    is_story=task.is_story,
                )
            elif task.media_type == "photo":
                if not task.photo_paths:
                    raise ValueError(f"[{task.task_id}] Не указан `photo_paths`.")
                srcs = [Path(p) for p in task.photo_paths]
                media_list = uniquify_photos(
                    srcs,
                    self.config.uniquifier,
                    rng=self._rng,
                    mode_override=task.photo_mode,
                )
                temp_files.extend(media_list)
                source_list = [str(s) for s in srcs]

                resp = await self.client.create_photo_post(
                    account,
                    media_list,
                    description=description,
                    description_mentions=mentions,
                    music_id=music_id,
                    region_override=task.region_override,
                    is_private=task.is_private,
                    disable_comments=task.disable_comments,
                    disable_duet=task.disable_duet,
                    disable_react=task.disable_react,
                    disable_stitch=task.disable_stitch,
                    is_story=task.is_story,
                )
            else:
                raise ValueError(f"Неизвестный media_type: {task.media_type}")
        finally:
            for m in temp_files:
                if m.is_temporary:
                    m.path.unlink(missing_ok=True)

        aweme_id = extract_aweme_id(resp, self.config.response_mapping)
        record = UploadedPostRecord(
            aweme_id=aweme_id,
            media_type=task.media_type,
            source_files=source_list,
            description=description,
            diagnostic_note=(
                ""
                if aweme_id
                else f"Пост отправлен (HTTP 200), но `aweme_id` не найден в ключах {list(resp.keys())}."
            ),
        )
        account.upload_history.append(record)
        self.store.save_account(account)

        task.completed = True
        task.completed_at = utc_now_iso()
        task.assigned_account_id = account.account_id
        task.published_aweme_id = aweme_id

        if appeal_after_upload:
            logger.info(
                "[%s] Запуск снятия ограничений после залива (settle=%s)...",
                account.account_id,
                settle_after_upload,
            )
            if settle_after_upload:
                await wait_and_appeal_settle(
                    account,
                    appeal_studio_posts=appeal_studio_posts,
                    hide_rejected=hide_rejected,
                    only_account_ineligible=not appeal_studio_posts,
                    verify_proxy_trace=False,
                )
            else:
                await remove_account_restrictions(
                    account,
                    appeal_studio_posts=appeal_studio_posts,
                    hide_rejected=hide_rejected,
                    only_account_ineligible=not appeal_studio_posts,
                    verify_proxy_trace=False,
                )
            self.store.save_account(account)

        return account, record

    # ── 5. Post-Upload Shadowban / 0-View Monitor ─────────────────────────────

    async def check_account_shadows(
        self, account: AccountRecord, *, zero_view_threshold_minutes: float = 45.0
    ) -> list[PostHealthReport]:
        await self.ensure_authenticated(account)
        self_posts_resp = await self.client.get_self_posts(account, count=20)
        self_items = extract_feed_items(self_posts_resp, self.config.response_mapping)
        self_ids = {
            aid
            for item in self_items
            if (aid := extract_aweme_id(item, self.config.response_mapping)) is not None
        }

        now = datetime.now(timezone.utc)
        reports: list[PostHealthReport] = []

        for rec in account.upload_history:
            if not rec.aweme_id:
                continue
            uploaded_dt = _parse_iso(rec.uploaded_at) or now
            age_min = max(0.0, (now - uploaded_dt).total_seconds() / 60.0)
            in_self = rec.aweme_id in self_ids

            in_public = False
            play_count: int | None = None
            try:
                post_resp = await self.client.get_post(account, rec.aweme_id)
                in_public = True
                play_count = extract_play_count(post_resp, self.config.response_mapping)
            except KintaroApiError:
                in_public = False

            suspected = False
            if not in_public and age_min >= 10.0:
                suspected = True
                verdict = "Скрыт/на модерации/удалён (`GET /v1/post/get` не отдал пост)"
            elif play_count == 0 and age_min >= zero_view_threshold_minutes:
                suspected = True
                verdict = f"Подозрение на теневой бан (0 просмотров спустя {age_min:.0f} мин)"
            elif play_count is not None and play_count > 0:
                verdict = f"В откруте ({play_count} просмотров)"
            else:
                verdict = "Слишком рано для выводов / счётчик не распознан в ответе"

            rec.last_checked_at = utc_now_iso()
            rec.last_play_count = play_count
            rec.suspected_shadowban = suspected
            rec.diagnostic_note = verdict

            reports.append(
                PostHealthReport(
                    account_id=account.account_id,
                    aweme_id=rec.aweme_id,
                    age_minutes=round(age_min, 1),
                    play_count=play_count,
                    found_in_self_posts=in_self,
                    found_in_public_get=in_public,
                    suspected_shadowban=suspected,
                    verdict=verdict,
                )
            )
            await self._human_pause(1.0, 2.5)

        self.store.save_account(account)
        return reports

    # ── 6. Cold TikTok App Restart & Old Posts Cleanup (merged from swegby) ──

    async def restart_tiktok_app(
        self,
        account: AccountRecord,
        *,
        target_region: str = "",
        app_locale: str = "",
    ) -> AccountRecord:
        """
        Simulates cold-closing and relaunching the Android TikTok app via Kintaro API:
          1) Rotates `session_ids.session`, `session_ids.request`, `start_time`, and `counters.sessions`
          2) Single fast `/v1/device/new_session` attempt (non-fatal)
          3) `/v1/device/update_hosts` cold-boot TNC refresh (non-fatal)
          4) `/v1/device/report {"type": "launch"}` (MSSDK `report_tk` refresh)
          5) `/v1/account/token_beat`
          6) `/v1/account/self`
          7) Re-applies target SIM/geo/locale profile and persists session
        """
        if account.device is None:
            raise RuntimeError(f"[{account.account_id}] Нет x-device для перезапуска TikTok.")

        dev_dict: dict[str, Any] = (
            dict(json.loads(account.device)) if isinstance(account.device, str) else dict(account.device)
        )
        sess_ids = dict(dev_dict.get("session_ids") or {})
        old_sid = str(sess_ids.get("session") or "")[:8]
        new_sid = str(uuid.uuid4())
        sess_ids["session"] = new_sid
        sess_ids["request"] = str(uuid.uuid4())
        dev_dict["session_ids"] = sess_ids
        dev_dict["start_time"] = int(time.time() * 1000)
        counters = dict(dev_dict.get("counters") or {})
        counters["sessions"] = int(counters.get("sessions") or 1) + 1
        dev_dict["counters"] = counters
        account.device = dev_dict

        logger.info(
            "[%s] 🔄 Холодный перезапуск TikTok (session %s.. -> %s.., сессий=%d)...",
            account.account_id,
            old_sid,
            new_sid[:8],
            counters["sessions"],
        )

        for label, coro in (
            ("new_session", self.client.new_session(account, max_retries_override=1)),
            ("update_hosts", self.client.update_hosts(account)),
            ("device/report(launch)", self.client.device_report(account, "launch")),
            ("token_beat", self.client.token_beat(account)),
            ("account/self", self.client.get_self(account)),
        ):
            try:
                await coro
            except Exception as exc:
                logger.warning("[%s] Предупреждение %s при перезапуске: %s", account.account_id, label, exc)

        eff_reg = (target_region or account.region).strip().upper()
        eff_loc = (app_locale or account.app_locale or "").strip() or None
        if account.device is not None and (eff_reg or eff_loc):
            account.device = apply_device_geo_profile(account.device, eff_reg, app_locale=eff_loc)
        account.last_restarted_at = utc_now_iso()
        account.last_token_beat_at = utc_now_iso()
        self.store.save_account(account)
        logger.info("[%s] ✅ Приложение TikTok перезапущено!", account.account_id)
        return account

    async def list_all_account_post_ids(
        self,
        account: AccountRecord,
        *,
        max_posts: int = 200,
    ) -> list[str]:
        """
        Collects ALL live post IDs on an account by uniting:
          1) TikTok Studio `/tiktok/creator/manage/item_list/v1/` (catches Private/Hidden/Under-Review posts at 0$ cost)
          2) Paginated Kintaro `GET /v1/account/posts` (`max_cursor` loop)
        """
        found: list[str] = []
        seen: set[str] = set()

        # 1) Studio item_list (non-fatal if cookies lack web studio access in mock/unit tests)
        try:
            studio_ids = await list_studio_post_ids(account, max_posts=max_posts)
            for vid in studio_ids:
                if vid and vid not in seen:
                    seen.add(vid)
                    found.append(vid)
        except Exception as exc:
            logger.debug("[%s] Studio item_list пропущен при сборе постов: %s", account.account_id, exc)

        # 2) Kintaro mobile API `/v1/account/posts` with cursor pagination
        cursor = "0"
        for _ in range(15):
            if len(found) >= max_posts:
                break
            try:
                resp = await self.client.get_self_posts(account, max_cursor=cursor, count=20)
            except Exception as exc:
                if found:
                    logger.warning("[%s] /v1/account/posts ошибка (используем найденные в Studio): %s", account.account_id, exc)
                    break
                raise
            items = extract_feed_items(resp, self.config.response_mapping)
            added = 0
            for it in items:
                aid = extract_aweme_id(it, self.config.response_mapping)
                if aid and aid not in seen:
                    seen.add(aid)
                    found.append(aid)
                    added += 1
            has_more = bool(resp.get("has_more"))
            next_cursor = str(resp.get("max_cursor") or "")
            if not has_more or added == 0 or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        return found

    async def delete_single_post(self, account: AccountRecord, aweme_id: str) -> tuple[bool, str]:
        """Deletes a single TikTok post via Kintaro `POST /v1/post/delete` and prunes local upload_history."""
        if not account.authenticated or account.device is None:
            await self.ensure_authenticated(account)
        target_id = str(aweme_id).strip()
        resp = await self.client.delete_post(account, target_id)
        sc = resp.get("status_code", 0)
        if sc != 0:
            return False, f"TikTok вернул status_code={sc}: {resp.get('status_msg') or resp}"
        account.upload_history = [p for p in account.upload_history if p.aweme_id != target_id]
        self.store.save_account(account)
        return True, f"Пост {target_id} полностью удалён"

    async def delete_all_account_posts(
        self,
        account: AccountRecord,
        *,
        keep_ids: set[str] | None = None,
        dry_run: bool = False,
    ) -> int:
        """
        Discovers ALL posts (Studio + Kintaro mobile API) and permanently deletes them via `/v1/post/delete`.
        Preserves any IDs in `keep_ids` and only prunes actually deleted posts from `account.upload_history`.
        """
        if not account.authenticated or account.device is None:
            await self.ensure_authenticated(account)

        skip = {str(x).strip() for x in (keep_ids or set()) if x}
        all_ids = [aid for aid in await self.list_all_account_post_ids(account) if aid not in skip]
        if dry_run:
            logger.info("[%s] [DRY-RUN] Найдено старых постов к удалению: %d", account.account_id, len(all_ids))
            return len(all_ids)

        deleted_ids: set[str] = set()
        for aid in all_ids:
            try:
                resp = await self.client.delete_post(account, aid)
                sc = resp.get("status_code", 0)
                if sc == 0:
                    deleted_ids.add(aid)
                    logger.info("[%s] 🗑 Удалён старый пост %s", account.account_id, aid)
                else:
                    logger.warning(
                        "[%s] Отказ удаления поста %s (status_code=%s: %s)",
                        account.account_id,
                        aid,
                        sc,
                        resp.get("status_msg") or "",
                    )
            except Exception as exc:
                logger.warning("[%s] Не удалось удалить пост %s: %s", account.account_id, aid, exc)
            await self._human_pause(0.4, 0.9)

        if deleted_ids:
            account.upload_history = [
                p for p in account.upload_history if p.aweme_id not in deleted_ids
            ]
            self.store.save_account(account)
        return len(deleted_ids)

    # ── 7. Multi-Threaded Anti-Shadow Campaign Runner (5 Threads + 3-Stage 2048) ──

    async def run_anti_shadow_campaign(
        self,
        campaign: CampaignConfig,
        *,
        progress_cb: Callable[[str, str, dict[str, Any]], None] | None = None,
        on_account_uploaded: Callable[[AccountRecord, CampaignAccountResult], None] | None = None,
    ) -> list[CampaignAccountResult]:
        """
        Full N-thread (default 5-thread) anti-shadowban campaign workflow:
          1. Optional sticky proxy pinning (only modifies proxy if explicitly enabled)
          2. Auth + target geo/locale profile (`US / en_US`, etc.)
          3. Optional old-post cleanup (`delete_all_account_posts`)
          4. Stage 1 Pre-Upload 2048 Auto-Appeal
          5. Upload N copies (`copies_count=6`) with `minimal_snow` uniquifier, randomized hashtags,
             unique Fortnite/keyword music track per post, Stage 2 Inter-Post 2048 Auto-Appeal after EVERY post,
             14-20s pacing, and Cold TikTok App Restart after every `restart_every_n` (3) posts
          6. Stage 3 Post-Upload 2048 Auto-Appeal (8s settle + 60s quiet window + 4-folder sorting)
          7. Live initial views snapshot
        """
        if not campaign.media_paths:
            raise ValueError("Не передано ни одного медиафайла для кампании (`media_paths`).")
        src_paths = [Path(p) for p in campaign.media_paths]
        for sp in src_paths:
            if not sp.exists():
                raise FileNotFoundError(f"Медиафайл кампании не найден: {sp}")

        target_accounts = (
            [self.store.load_account(aid) for aid in campaign.account_ids]
            if campaign.account_ids
            else self.store.list_accounts()
        )
        if not target_accounts:
            raise RuntimeError("Нет аккаунтов для запуска кампании.")

        sem = asyncio.Semaphore(max(1, campaign.threads))
        state_dir = Path(self.config.state_dir)

        def _emit(aid: str, msg: str, meta: dict[str, Any] | None = None) -> None:
            logger.info("[%s] %s", aid, msg)
            if progress_cb:
                progress_cb(aid, msg, meta or {})

        async def _run_one(idx: int, acc: AccountRecord) -> CampaignAccountResult:
            if idx < campaign.threads and campaign.threads > 1:
                await self._sleep(idx * 1.2)

            async with sem:
                t0 = time.monotonic()
                res = CampaignAccountResult(
                    account_id=acc.account_id,
                    username=acc.username or acc.account_id,
                    status="running",
                    target_posts=campaign.copies_count,
                )
                eff_region = (campaign.region or acc.region or "US").upper().strip()
                eff_phone_reg = (campaign.phone_region or eff_region).upper().strip()
                eff_locale = (
                    campaign.app_locale
                    or acc.app_locale
                    or GEO_LOCALE_DEFAULTS.get(eff_phone_reg, "en_US")
                ).strip()

                try:
                    # 1) Optional sticky proxy pinning (never changes `_country-VN` unless override_proxy_country is set!)
                    if campaign.pin_sticky_proxy or campaign.override_proxy_country:
                        acc.proxy = allocate_sticky_proxy(
                            acc.proxy,
                            target_country=campaign.override_proxy_country,
                            lifetime="60m",
                            preserve_existing_session=True,
                        )
                        self.store.save_account(acc)

                    # 2) Authenticate if needed
                    _emit(acc.account_id, f"Старт кампании ({eff_region}/{eff_locale}, уник={campaign.photo_mode})...")
                    await self.ensure_authenticated(acc)
                    res.username = acc.username or acc.account_id
                    if acc.device is not None:
                        acc.device = apply_device_geo_profile(acc.device, eff_phone_reg, app_locale=eff_locale)
                        acc.app_locale = eff_locale
                        self.store.save_account(acc)

                    # 3) Clean old posts
                    if campaign.delete_old_posts:
                        _emit(acc.account_id, "Очистка старых видео перед заливом...")
                        try:
                            res.deleted_old_posts = await self.delete_all_account_posts(acc)
                        except Exception as exc:
                            _emit(acc.account_id, f"Предупреждение очистки старых постов: {exc}")

                    # 4) Stage 1: Pre-upload 2048 Auto-Appeal
                    if campaign.auto_appeal_2048 and campaign.appeal_pre_upload:
                        _emit(acc.account_id, "🛡️ [Этап 1/3] Пред-залив Auto-Appeal 2048...")
                        try:
                            pre_ap = await remove_account_restrictions(
                                acc,
                                appeal_text=campaign.appeal_text,
                                appeal_studio_posts=False,
                                only_account_ineligible=True,
                                verify_proxy_trace=False,
                            )
                            res.appeals_sent += pre_ap.notice_appeals_verified
                        except Exception as exc:
                            _emit(acc.account_id, f"Предупреждение пред-залив Auto-Appeal: {exc}")

                    if campaign.warmup_before_upload:
                        _emit(acc.account_id, "🔥 Прогрев FYP перед заливом...")
                        try:
                            await self.run_warmup(acc)
                        except Exception as exc:
                            _emit(acc.account_id, f"Предупреждение прогрева: {exc}")

                    # 5) Music pool for distinct track per post
                    music_pool: list[dict[str, str]] = []
                    used_music_ids: set[str] = set()
                    if not campaign.music_id.strip() and campaign.auto_music_query.strip():
                        music_pool = await self.client.build_music_pool(
                            acc, keyword=campaign.auto_music_query.strip(), region_override=eff_region
                        )
                        _emit(acc.account_id, f"🎵 Подобран пул треков ({campaign.auto_music_query}): {len(music_pool)} шт.")

                    # 6) Publish N copies with retry recovery + inter-post 2048 appeal + cold restart every N
                    used_captions: set[str] = set()
                    restarted_at_marks: set[int] = set()

                    for attempt in range(1, campaign.max_upload_retries + 1):
                        if res.posts_published >= campaign.copies_count:
                            break
                        if attempt > 1:
                            _emit(
                                acc.account_id,
                                f"🔄 Дозалив оставшихся {campaign.copies_count - res.posts_published} постов (попытка {attempt}/{campaign.max_upload_retries})...",
                            )

                        while res.posts_published < campaign.copies_count:
                            copy_num = res.posts_published + 1
                            desc, mentions = prepare_description_and_mentions(
                                campaign.description,
                                campaign.mentions,
                                rng=self._rng,
                                shuffle_tags=campaign.randomize_tags,
                                used_captions=used_captions,
                            )

                            chosen_music_id = campaign.music_id.strip()
                            if not chosen_music_id and music_pool:
                                avail = [t for t in music_pool if t["id"] not in used_music_ids] or list(music_pool)
                                picked = self._rng.choice(avail)
                                used_music_ids.add(picked["id"])
                                chosen_music_id = picked["id"]

                            temp_files: list[PreparedMedia] = []
                            try:
                                if campaign.media_type == "video":
                                    med = uniquify_video(src_paths[0], self.config.uniquifier, rng=self._rng)
                                    temp_files.append(med)
                                    resp = await self.client.create_video_post(
                                        acc,
                                        med,
                                        description=desc,
                                        description_mentions=mentions,
                                        music_id=chosen_music_id,
                                        region_override=eff_region,
                                        is_private=campaign.is_private,
                                        disable_comments=campaign.disable_comments,
                                        is_story=campaign.is_story,
                                    )
                                else:
                                    med_list = uniquify_photos(
                                        src_paths,
                                        self.config.uniquifier,
                                        rng=self._rng,
                                        mode_override=campaign.photo_mode,
                                        overrides=campaign.uniquifier_overrides or None,
                                        copy_index=copy_num - 1,
                                    )
                                    temp_files.extend(med_list)
                                    try:
                                        resp = await self.client.create_photo_post(
                                            acc,
                                            med_list,
                                            description=desc,
                                            description_mentions=mentions,
                                            music_id=chosen_music_id,
                                            region_override=eff_region,
                                            is_private=campaign.is_private,
                                            disable_comments=campaign.disable_comments,
                                            is_story=campaign.is_story,
                                        )
                                    except KintaroApiError:
                                        if chosen_music_id and not campaign.music_id.strip():
                                            alt = next(
                                                (t["id"] for t in music_pool if t["id"] != chosen_music_id and t["id"] not in used_music_ids),
                                                "",
                                            )
                                            resp = await self.client.create_photo_post(
                                                acc,
                                                med_list,
                                                description=desc,
                                                description_mentions=mentions,
                                                music_id=alt,
                                                region_override=eff_region,
                                                is_private=campaign.is_private,
                                                disable_comments=campaign.disable_comments,
                                                is_story=campaign.is_story,
                                            )
                                        else:
                                            raise
                            except Exception as u_exc:
                                _emit(acc.account_id, f"⚠️ Сбой на посте {copy_num}/{campaign.copies_count}: {u_exc}")
                                await self._sleep(2.5)
                                break
                            finally:
                                for mf in temp_files:
                                    if mf.is_temporary:
                                        mf.path.unlink(missing_ok=True)

                            if acc.device is not None:
                                acc.device = apply_device_geo_profile(acc.device, eff_phone_reg, app_locale=eff_locale)

                            aweme_id = extract_aweme_id(resp, self.config.response_mapping) or ""
                            kind_seg = "video" if campaign.media_type == "video" else "photo"
                            post_url = (
                                f"https://www.tiktok.com/@{acc.username or acc.account_id}/{kind_seg}/{aweme_id}"
                                if aweme_id
                                else ""
                            )
                            acc.upload_history.append(
                                UploadedPostRecord(
                                    aweme_id=aweme_id or None,
                                    media_type=campaign.media_type,
                                    source_files=[str(s) for s in src_paths],
                                    description=desc,
                                )
                            )
                            self.store.save_account(acc)

                            res.posts_published += 1
                            if aweme_id:
                                res.aweme_ids.append(aweme_id)
                            if post_url:
                                res.post_urls.append(post_url)
                            _emit(
                                acc.account_id,
                                f"✅ Пост [{res.posts_published}/{campaign.copies_count}] опубликован: {post_url or aweme_id}",
                                {"published": res.posts_published, "aweme_id": aweme_id},
                            )

                            # Inter-post 2048 hook + pacing + Cold TikTok App Restart every N posts
                            if res.posts_published < campaign.copies_count:
                                t_hook0 = time.monotonic()
                                if campaign.auto_appeal_2048 and campaign.appeal_inter_post:
                                    await self._sleep(3.0)
                                    try:
                                        mid_ap = await remove_account_restrictions(
                                            acc,
                                            post_ids=list(res.aweme_ids),
                                            appeal_text=campaign.appeal_text,
                                            appeal_studio_posts=False,
                                            only_account_ineligible=True,
                                            verify_proxy_trace=False,
                                        )
                                        if mid_ap.notice_appeals_verified > 0:
                                            res.appeals_sent += mid_ap.notice_appeals_verified
                                            _emit(
                                                acc.account_id,
                                                f"⚡ Межпостовой 2048 Auto-Appeal: +{mid_ap.notice_appeals_verified} (всего: {res.appeals_sent})",
                                            )
                                    except Exception as m_exc:
                                        _emit(acc.account_id, f"⚠️ Межпостовой чек 2048: {m_exc}")

                                hook_dt = time.monotonic() - t_hook0
                                if campaign.delay_max_sec > 0:
                                    target_pause = self._rng.uniform(
                                        campaign.delay_min_sec,
                                        max(campaign.delay_min_sec, campaign.delay_max_sec),
                                    )
                                    rem_pause = max(0.0, target_pause - hook_dt)
                                    if rem_pause > 0.2:
                                        await self._sleep(rem_pause)

                                if (
                                    campaign.restart_every_n > 0
                                    and res.posts_published % campaign.restart_every_n == 0
                                    and res.posts_published not in restarted_at_marks
                                ):
                                    restarted_at_marks.add(res.posts_published)
                                    await self.restart_tiktok_app(
                                        acc, target_region=eff_phone_reg, app_locale=eff_locale
                                    )

                    # 7) Stage 3: Post-upload 2048 wave settle + 4-folder sorting
                    #    OR immediate handoff to dedicated trailing-thread UnbanWaveQueueManager
                    final_audit = None
                    deferred_to_queue = (
                        campaign.auto_appeal_2048
                        and campaign.appeal_post_upload
                        and campaign.defer_post_appeal_to_queue
                        and on_account_uploaded is not None
                    )
                    if deferred_to_queue:
                        res.folder_key = "queued_unban"
                        res.appeal_verdict = "Передан в очередь дожима 2048"
                        _emit(
                            acc.account_id,
                            "🔄 Залив готов — передаю аккаунт в отдельные потоки дожима волн 2048 (поток залива освобождается сразу)...",
                        )
                    elif campaign.auto_appeal_2048 and campaign.appeal_post_upload:
                        if campaign.appeal_post_wait_sec > 0 and res.posts_published > 0:
                            await self._sleep(campaign.appeal_post_wait_sec)
                        _emit(
                            acc.account_id,
                            f"🛡️ [Этап 3/3] Пост-залив Auto-Appeal 2048 (тишина {int(campaign.appeal_quiet_window_sec)}с)...",
                        )
                        try:
                            post_ap = await wait_and_appeal_settle(
                                acc,
                                appeal_text=campaign.appeal_text,
                                appeal_studio_posts=False,
                                only_account_ineligible=True,
                                verify_proxy_trace=False,
                                quiet_seconds=campaign.appeal_quiet_window_sec,
                                poll_interval_seconds=6.0,
                            )
                            res.appeals_sent += post_ap.notice_appeals_verified
                            final_audit = post_ap.audit
                        except Exception as p_exc:
                            _emit(acc.account_id, f"⚠️ Пост-залив Auto-Appeal ошибка: {p_exc}")

                    if not deferred_to_queue:
                        folder_key, folder_title = classify_appeal_folder(final_audit, had_error=False)
                        res.folder_key = folder_key
                        res.appeal_verdict = folder_title
                        if campaign.sort_to_folders:
                            sort_account_into_folder(state_dir, folder_key, acc)

                    # Quick live views snapshot
                    try:
                        sp_resp = await self.client.get_self_posts(acc, count=12)
                        sp_items = extract_feed_items(sp_resp, self.config.response_mapping)
                        res.initial_views = sum(
                            int(extract_play_count(it, self.config.response_mapping) or 0) for it in sp_items
                        )
                    except Exception:
                        pass

                    res.status = (
                        "completed"
                        if res.posts_published >= campaign.copies_count
                        else ("partial" if res.posts_published > 0 else "upload_failed")
                    )
                    res.elapsed_sec = round(time.monotonic() - t0, 1)
                    if deferred_to_queue and on_account_uploaded is not None:
                        try:
                            on_account_uploaded(acc, res)
                        except Exception as cb_exc:
                            _emit(acc.account_id, f"⚠️ Не удалось поставить в очередь дожима 2048: {cb_exc}")
                    _emit(
                        acc.account_id,
                        f"🏁 Кампания завершена за {res.elapsed_sec}с: постов={res.posts_published}/{campaign.copies_count}, "
                        f"апелляций={res.appeals_sent}, папка={res.folder_key}, просмотры={res.initial_views}",
                        {"status": res.status, "folder_key": res.folder_key},
                    )
                    return res

                except Exception as exc:
                    res.status = "error"
                    res.error = str(exc)
                    res.folder_key = "errors"
                    res.elapsed_sec = round(time.monotonic() - t0, 1)
                    if campaign.sort_to_folders:
                        try:
                            sort_account_into_folder(state_dir, "errors", acc, note=str(exc)[:120])
                        except Exception:
                            pass
                    _emit(acc.account_id, f"❌ Ошибка кампании: {exc}", {"status": "error"})
                    return res

        return list(await asyncio.gather(*(_run_one(i, a) for i, a in enumerate(target_accounts))))

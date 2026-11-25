from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.parse
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kintaro_auto.captions import prepare_description_and_mentions
from kintaro_auto.client import (
    AuthChallengeRequiredError,
    KintaroApiError,
    KintaroClient,
)
from kintaro_auto.config import AppConfig
from kintaro_auto.models import (
    AccountRecord,
    PostHealthReport,
    UploadedPostRecord,
    UploadTask,
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

        login_region = (account.auth_region or "DE" if account.totp_token and not account.auth_region else (account.auth_region or account.region)).upper()

        # Step A: Register sticky device if none exists yet
        if account.device is None:
            await self._register_and_boot_device(account, login_region)

        # Step B: Sign in if not yet authenticated
        if not account.authenticated:
            if account.auth_method == "device_only":
                await self.client.get_self(account)
                account.authenticated = True
                account.first_authenticated_at = account.first_authenticated_at or utc_now_iso()
                self.store.save_account(account)
                return account

            pre_outlook_token = ""
            known_old_ids: set[str] | None = None
            if account.use_outlook_hub and account.email and account.mail_password:
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
                            timeout_sec=90,
                            poll_interval_sec=1.0,
                            log_fn=lambda m: logger.info("[%s] %s", account.account_id, m),
                        )
                        return await self.confirm_signin_code(account, otp_code)

                    if code_provider is None:
                        raise
                    code = code_provider(account)
                    return await self.confirm_signin_code(account, code)
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

    async def confirm_signin_code(self, account: AccountRecord, code: str) -> AccountRecord:
        if account.auth_method == "username":
            await self.client.signin_username_send_code(account, code)
        elif account.auth_method == "email":
            try:
                await self.client.signin_email_send_code(account, code)
            except KintaroApiError:
                if account.username and account.password and account.pending_verify:
                    logger.info(
                        "[%s] Повтор отправки кода через /v1/auth/signin/username/send_code...",
                        account.account_id,
                    )
                    await self.client.signin_username_send_code(account, code)
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
    ) -> tuple[AccountRecord, UploadedPostRecord]:
        account = self.select_account_for_task(task, force=force_cooldown)
        await self.ensure_authenticated(account)

        # Re-check eligibility after potential fresh first-time authentication!
        ok, reason = self.check_posting_eligibility(account, force=force_cooldown)
        if not ok:
            raise RuntimeError(f"[{account.account_id}] Залив остановлен защитой от теней: {reason}")

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

        # Prepare caption, spintax, and trailing-space mentions per Kintaro spec
        description, mentions = prepare_description_and_mentions(
            task.description, task.mentions, rng=self._rng
        )

        # Resolve optional music search
        music_id = task.music_id.strip()
        if not music_id and task.music_search_keyword:
            music_resp = await self.client.search_music(
                account, keyword=task.music_search_keyword
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
                    srcs, self.config.uniquifier, rng=self._rng
                )
                temp_files.extend(media_list)
                source_list = [str(s) for s in srcs]

                resp = await self.client.create_photo_post(
                    account,
                    media_list,
                    description=description,
                    description_mentions=mentions,
                    music_id=music_id,
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

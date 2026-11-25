from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
import httpx

from kintaro_auto.config import AppConfig
from kintaro_auto.models import AccountRecord
from kintaro_auto.response_adapters import extract_device_update, extract_verify_object
from kintaro_auto.uniquifier import PreparedMedia


class KintaroApiError(RuntimeError):
    def __init__(self, status_code: int, endpoint: str, payload: Any) -> None:
        self.status_code = status_code
        self.endpoint = endpoint
        self.payload = payload
        super().__init__(f"Kintaro API HTTP {status_code} на {endpoint}: {payload}")


class AuthChallengeRequiredError(KintaroApiError):
    """HTTP 403 on signin/signup: confirmation code required (`send_code`)."""

    def __init__(
        self,
        endpoint: str,
        payload: Any,
        verify: dict[str, Any] | None,
    ) -> None:
        super().__init__(403, endpoint, payload)
        self.verify = verify


class KintaroClient:
    def __init__(
        self,
        config: AppConfig,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.api_key = config.resolve_api_key()
        self._client = httpx.AsyncClient(
            base_url=config.api.base_url.rstrip("/"),
            timeout=httpx.Timeout(config.api.timeout_seconds),
            headers={"x-api-key": self.api_key},
            transport=transport,
        )

    async def __aenter__(self) -> KintaroClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def _dump_raw_log(
        self,
        account_id: str,
        method: str,
        endpoint: str,
        status_code: int,
        payload: Any,
    ) -> None:
        if not self.config.api.save_raw_responses:
            return
        log_dir = Path(self.config.api.raw_logs_dir) / account_id
        log_dir.mkdir(parents=True, exist_ok=True)
        slug = endpoint.strip("/").replace("/", "_")
        ts_ms = int(time.time() * 1000)
        out = log_dir / f"{ts_ms}_{method}_{slug}_{status_code}.json"
        with out.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "method": method,
                    "endpoint": endpoint,
                    "status_code": status_code,
                    "response": payload,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

    async def _request(
        self,
        method: str,
        endpoint: str,
        *,
        account: AccountRecord,
        include_device: bool = True,
        require_device: bool = True,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        allow_auth_403: bool = False,
        is_device_endpoint: bool = False,
        proxy_override: str | None = None,
        max_retries_override: int | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {"x-proxy": proxy_override or account.proxy}
        if include_device:
            if account.device is not None:
                headers["x-device"] = account.device_header_value()
            elif require_device:
                raise RuntimeError(
                    f"[{account.account_id}] Эндпоинт {endpoint} требует заголовок `x-device`, но устройство ещё не инициализировано."
                )

        last_exc: Exception | None = None
        # Transient 500/502/503/504 (such as Kintaro's `Failed build ri_report.`) fail before Passport
        # and do not consume auth balance; retry up to max_retries.
        attempts = max(1, max_retries_override if max_retries_override is not None else self.config.api.max_retries)

        for attempt in range(1, attempts + 1):
            try:
                resp = await self._client.request(
                    method,
                    endpoint,
                    headers=headers,
                    params=params,
                    json=json_body,
                    data=data,
                    files=files,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < attempts:
                    await asyncio.sleep(self.config.api.retry_backoff_seconds * attempt)
                    continue
                raise RuntimeError(
                    f"[{account.account_id}] Сетевая ошибка при запросе {method} {endpoint}: {exc}"
                ) from exc

            if resp.status_code >= 500 and attempt < attempts:
                await asyncio.sleep(self.config.api.retry_backoff_seconds * attempt)
                continue

            try:
                payload = resp.json()
            except ValueError:
                payload = {"raw_text": resp.text}

            self._dump_raw_log(
                account.account_id, method, endpoint, resp.status_code, payload
            )

            # Persist any updated `x-device` returned via header or JSON body (even on 403 auth challenge)
            resp_headers = dict(resp.headers.items())
            updated_dev = extract_device_update(
                payload,
                resp_headers,
                self.config.response_mapping,
                is_device_endpoint=is_device_endpoint,
            )
            if updated_dev is not None:
                account.device = updated_dev

            if resp.status_code == 403 and allow_auth_403:
                verify_obj = extract_verify_object(payload, self.config.response_mapping)
                account.pending_verify = verify_obj
                raise AuthChallengeRequiredError(endpoint, payload, verify_obj)

            if resp.status_code >= 400:
                raise KintaroApiError(resp.status_code, endpoint, payload)

            return payload if isinstance(payload, dict) else {"data": payload}

        raise RuntimeError(f"Не удалось выполнить запрос {endpoint}: {last_exc}")

    async def _sync_totp_window_if_needed(self, account: AccountRecord) -> None:
        if not account.totp_token or not self.config.api.sync_totp_window:
            return
        min_rem = self.config.api.totp_min_remaining_seconds
        if min_rem <= 0:
            return
        rem = 30.0 - (time.time() % 30.0)
        if rem < min_rem:
            wait_s = rem + 0.5
            await asyncio.sleep(wait_s)

    # ── Device ────────────────────────────────────────────────────────────────

    async def register_device(
        self,
        account: AccountRecord,
        *,
        region_override: str | None = None,
        proxy_override: str | None = None,
    ) -> dict[str, Any]:
        res = await self._request(
            "POST",
            "/v1/device/register",
            account=account,
            include_device=False,
            require_device=False,
            json_body={"region": region_override or account.region},
            is_device_endpoint=True,
            proxy_override=proxy_override,
        )
        if account.device is None:
            raise RuntimeError(
                f"[{account.account_id}] `/v1/device/register` вернул 200, но структура `x-device` не распознана. "
                f"Ключи ответа: {list(res.keys())}. Проверь `response_mapping.device_keys` в config.toml."
            )
        return res

    async def update_hosts(
        self, account: AccountRecord, *, proxy_override: str | None = None
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/device/update_hosts",
            account=account,
            proxy_override=proxy_override,
        )

    async def new_session(
        self,
        account: AccountRecord,
        *,
        proxy_override: str | None = None,
        max_retries_override: int | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/device/new_session",
            account=account,
            proxy_override=proxy_override,
            max_retries_override=max_retries_override,
        )

    async def device_report(self, account: AccountRecord, report_type: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/device/report",
            account=account,
            json_body={"type": report_type},
        )

    # ── Signin (Existing Accounts) ────────────────────────────────────────────

    async def signin_username(
        self,
        account: AccountRecord,
        *,
        region_override: str | None = None,
        proxy_override: str | None = None,
    ) -> dict[str, Any]:
        if not account.username or not account.password:
            raise ValueError(
                f"[{account.account_id}] Для `signin/username` необходимы `username` и `password`."
            )
        await self._sync_totp_window_if_needed(account)
        body: dict[str, Any] = {
            "region": region_override or account.region,
            "username": account.username,
            "password": account.password,
        }
        if account.totp_token:
            body["totp_token"] = account.totp_token
        return await self._request(
            "POST",
            "/v1/auth/signin/username",
            account=account,
            require_device=True,
            json_body=body,
            allow_auth_403=True,
            proxy_override=proxy_override,
        )

    async def signin_username_send_code(
        self, account: AccountRecord, code: str
    ) -> dict[str, Any]:
        if not account.pending_verify:
            raise RuntimeError(
                f"[{account.account_id}] Нет сохранённого объекта `verify` от предыдущего шага входа (403)."
            )
        body = {
            "username": account.username,
            "password": account.password,
            "code": code.strip(),
            "verify": account.pending_verify,
        }
        return await self._request(
            "POST",
            "/v1/auth/signin/username/send_code",
            account=account,
            json_body=body,
        )

    async def signin_email(
        self,
        account: AccountRecord,
        *,
        region_override: str | None = None,
        proxy_override: str | None = None,
    ) -> dict[str, Any]:
        if not account.email:
            raise ValueError(
                f"[{account.account_id}] Для `signin/email` необходимо поле `email`."
            )
        await self._sync_totp_window_if_needed(account)
        body: dict[str, Any] = {
            "region": region_override or account.region,
            "email": account.email,
        }
        if account.password:
            body["password"] = account.password
        if account.totp_token:
            body["totp_token"] = account.totp_token
        return await self._request(
            "POST",
            "/v1/auth/signin/email",
            account=account,
            require_device=True,
            json_body=body,
            allow_auth_403=True,
            proxy_override=proxy_override,
        )

    async def signin_email_send_code(
        self, account: AccountRecord, code: str
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "email": account.email,
            "code": code.strip(),
        }
        if account.password:
            body["password"] = account.password
        try:
            return await self._request(
                "POST",
                "/v1/auth/signin/email/send_code",
                account=account,
                json_body=body,
            )
        except KintaroApiError:
            if not account.pending_verify:
                raise
            norm_verify = dict(account.pending_verify)
            norm_verify.setdefault("device_redirect_info", "")
            norm_verify.setdefault("challenge_type", "2")
            body_with_verify = dict(body)
            body_with_verify["verify"] = norm_verify
            return await self._request(
                "POST",
                "/v1/auth/signin/email/send_code",
                account=account,
                json_body=body_with_verify,
            )

    async def signin_mobile(self, account: AccountRecord) -> dict[str, Any]:
        if not account.mobile:
            raise ValueError(
                f"[{account.account_id}] Для `signin/mobile` необходимо поле `mobile`."
            )
        body = {
            "region": account.region,
            "mobile": account.mobile,
        }
        return await self._request(
            "POST",
            "/v1/auth/signin/mobile",
            account=account,
            require_device=False,
            json_body=body,
            allow_auth_403=True,
        )

    async def signin_mobile_send_code(
        self, account: AccountRecord, code: str
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "mobile": account.mobile,
            "code": code.strip(),
        }
        if account.password:
            body["password"] = account.password
        return await self._request(
            "POST",
            "/v1/auth/signin/mobile/send_code",
            account=account,
            json_body=body,
        )

    # ── Account ───────────────────────────────────────────────────────────────

    async def token_beat(self, account: AccountRecord) -> dict[str, Any]:
        return await self._request("POST", "/v1/account/token_beat", account=account)

    async def get_self(self, account: AccountRecord) -> dict[str, Any]:
        return await self._request("GET", "/v1/account/self", account=account)

    async def get_self_posts(
        self,
        account: AccountRecord,
        max_cursor: str = "0",
        min_cursor: str = "0",
        count: int = 9,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/account/posts",
            account=account,
            params={
                "max_cursor": max_cursor,
                "min_cursor": min_cursor,
                "count": str(count),
            },
        )

    async def set_nickname(self, account: AccountRecord, nickname: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/account/set_nickname",
            account=account,
            json_body={"nickname": nickname},
        )

    async def set_bio(
        self, account: AccountRecord, text: str, mentions: list[str] | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"text": text}
        if mentions:
            body["mentions"] = mentions
        return await self._request(
            "POST", "/v1/account/set_bio", account=account, json_body=body
        )

    async def set_bio_url(self, account: AccountRecord, url: str) -> dict[str, Any]:
        return await self._request(
            "POST", "/v1/account/set_bio_url", account=account, json_body={"url": url}
        )

    async def set_avatar(self, account: AccountRecord, image_base64: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/account/set_avatar",
            account=account,
            json_body={"image": image_base64},
        )

    async def change_username(self, account: AccountRecord, username: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/account/change_username",
            account=account,
            json_body={"username": username.lstrip("@").strip()},
        )

    async def set_password(self, account: AccountRecord, password: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/account/set_password",
            account=account,
            json_body={"password": password},
        )

    async def enable_totp(
        self, account: AccountRecord, additional_way: str = "pwd"
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/account/enable_totp",
            account=account,
            json_body={"additional_way": additional_way},
        )

    # ── Feed, Inbox, Music & User (Warm-up & Discovery) ───────────────────────

    async def get_recommendations(
        self,
        account: AccountRecord,
        pull_type: str = "0",
        region: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {"pull_type": pull_type}
        if region or account.region:
            params["region"] = region or account.region
        return await self._request(
            "GET",
            "/v1/feed/recommendations",
            account=account,
            params=params,
        )

    async def get_suggested_users(
        self, account: AccountRecord, page_token: str = "", count: int = 30
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/inbox/suggested_users",
            account=account,
            params={"page_token": page_token, "count": str(count)},
        )

    async def search_music(
        self,
        account: AccountRecord,
        keyword: str,
        cursor: str = "0",
        count: int = 10,
        *,
        region_override: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/music/search",
            account=account,
            params={
                "keyword": keyword,
                "cursor": cursor,
                "count": str(count),
                "region": (region_override or account.region).upper(),
            },
        )

    async def get_music_chart(
        self,
        account: AccountRecord,
        cursor: str = "0",
        count: int = 30,
        *,
        region_override: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/music/chart",
            account=account,
            params={
                "cursor": cursor,
                "count": str(count),
                "region": (region_override or account.region).upper(),
            },
        )

    async def build_music_pool(
        self,
        account: AccountRecord,
        keyword: str = "Fortnite",
        *,
        region_override: str | None = None,
    ) -> list[dict[str, str]]:
        """
        Builds a deduplicated pool of playable TikTok music tracks (`id`, `title`, `author`)
        combining live `/v1/music/search`, `/v1/music/chart`, and verified fallbacks.
        """
        eff_region = (region_override or account.region).upper().strip()
        pool: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        non_en_markers = (
            "alkuperäinen", "оригинальный", "orijinal", "dźwięk",
            "suono originale", "son original", "som original",
            "originalton", "sunet original", "originální",
        )

        def _add(m: Any) -> None:
            if not isinstance(m, dict):
                return
            if isinstance(m.get("music"), dict):
                m = m["music"]
            mid = str(m.get("id_str") or m.get("mid") or m.get("id") or "").strip()
            if not mid or not mid.isdigit() or mid in seen_ids:
                return
            if m.get("mute_share") or (m.get("prevent_download") is True and m.get("music_status") == 0):
                return
            if int(m.get("duration") or 15) < 6:
                return
            title_str = str(m.get("title") or "Original Sound").strip()
            author_str = str(m.get("author") or "TikTok").strip()
            if eff_region in ("US", "GB", "CA", "AU") and any(b in title_str.lower() for b in non_en_markers):
                return
            seen_ids.add(mid)
            pool.append({"id": mid, "title": title_str, "author": author_str})

        kw = (keyword or "").strip()
        if kw and kw.lower() not in ("recommended", "chart", "auto", "предложенные"):
            try:
                res = await self.search_music(account, keyword=kw, count=25, region_override=eff_region)
                for entry in res.get("music_info_list") or res.get("music") or []:
                    if isinstance(entry, dict):
                        for sub in entry.get("musics") or []:
                            _add(sub)
                        _add(entry.get("music") or entry)
            except Exception:
                pass

        if len(pool) < 6:
            try:
                chart = await self.get_music_chart(account, count=25, region_override=eff_region)
                for entry in chart.get("music_list") or chart.get("music_info_list") or chart.get("data") or []:
                    _add(entry)
            except Exception:
                pass

        if len(pool) < 6 and kw.lower() == "fortnite":
            for fb in (
                {"id": "7436971510241167377", "title": "Fortnite Og Mode", "author": "Aye Brown"},
                {"id": "6891415646903470081", "title": "Fortnite - Battle Royale - Original Main Theme", "author": "Geek Music"},
                {"id": "6782722012662990849", "title": "Smooth Moves", "author": "Fortnite"},
                {"id": "6902489124184262657", "title": "Fortnite", "author": "Trap Baby"},
                {"id": "7289836584265255686", "title": "OG music", "author": "Fortnite_MusicPack"},
                {"id": "7253513478835620635", "title": "Lobby Classic", "author": "Fortnite Music!"},
            ):
                if fb["id"] not in seen_ids:
                    seen_ids.add(fb["id"])
                    pool.append(fb)
        return pool

    async def get_user_profile(self, account: AccountRecord, username: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/user/get",
            account=account,
            params={"username": username.lstrip("@").strip()},
        )

    async def get_user_posts(
        self,
        account: AccountRecord,
        username: str,
        count: int = 18,
        max_cursor: str = "0",
        *,
        region_override: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str] = {
            "username": username.lstrip("@").strip(),
            "count": str(max(1, min(35, int(count)))),
            "max_cursor": str(max_cursor),
        }
        if region_override or account.region:
            params["region"] = (region_override or account.region).upper()
        return await self._request(
            "GET",
            "/v1/user/posts",
            account=account,
            params=params,
        )

    async def follow_user(self, account: AccountRecord, username: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/user/follow",
            account=account,
            json_body={"username": username},
        )

    # ── Post & Interactions ───────────────────────────────────────────────────

    async def view_post(
        self, account: AccountRecord, aweme_id: str, playtime_ms: int
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/post/view",
            account=account,
            json_body={"aweme_id": str(aweme_id), "playtime": str(playtime_ms)},
        )

    async def like_post(self, account: AccountRecord, aweme_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/post/like",
            account=account,
            json_body={"aweme_id": str(aweme_id)},
        )

    async def collect_post(self, account: AccountRecord, aweme_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/post/collect",
            account=account,
            json_body={"aweme_id": str(aweme_id)},
        )

    async def repost_post(self, account: AccountRecord, aweme_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/post/repost",
            account=account,
            json_body={"aweme_id": str(aweme_id)},
        )

    async def delete_post(self, account: AccountRecord, aweme_id: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/post/delete",
            account=account,
            json_body={"aweme_id": str(aweme_id).strip()},
        )

    async def change_post_visibility(
        self, account: AccountRecord, aweme_id: str, visibility_type: int = 1
    ) -> dict[str, Any]:
        """POST /v1/post/change_visibility (`1` = Public, `2` = Private, `3` = Friends)."""
        return await self._request(
            "POST",
            "/v1/post/change_visibility",
            account=account,
            json_body={"aweme_id": str(aweme_id).strip(), "type": int(visibility_type)},
        )

    async def publish_comment(
        self,
        account: AccountRecord,
        aweme_id: str,
        text: str = "",
        *,
        mentions: list[str] | None = None,
        reply_id: str = "",
        reply_to_reply_id: str = "",
        image_base64: str = "",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"aweme_id": str(aweme_id).strip()}
        if text:
            payload["text"] = text
        if mentions:
            payload["mentions"] = [m.lstrip("@").strip() for m in mentions if m.strip()]
        if reply_id:
            payload["reply_id"] = str(reply_id).strip()
        if reply_to_reply_id:
            payload["reply_to_reply_id"] = str(reply_to_reply_id).strip()
        if image_base64:
            payload["image_base64"] = image_base64
        return await self._request(
            "POST",
            "/v1/comment/publish",
            account=account,
            json_body=payload,
        )

    async def like_comment(
        self, account: AccountRecord, aweme_id: str, cid: str
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/v1/comment/like",
            account=account,
            json_body={"aweme_id": str(aweme_id).strip(), "cid": str(cid).strip()},
        )

    async def get_post_comments(
        self,
        account: AccountRecord,
        aweme_id: str,
        cursor: str = "0",
        count: int = 8,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/post/comments",
            account=account,
            params={
                "aweme_id": str(aweme_id),
                "cursor": cursor,
                "count": str(count),
                "region": account.region,
            },
        )

    async def get_post(self, account: AccountRecord, aweme_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/v1/post/get",
            account=account,
            params={"aweme_id": str(aweme_id), "region": account.region},
        )

    async def create_video_post(
        self,
        account: AccountRecord,
        media: PreparedMedia,
        *,
        description: str = "",
        description_mentions: list[str] | None = None,
        music_id: str = "",
        region_override: str | None = None,
        is_private: bool = False,
        disable_comments: bool = False,
        disable_duet: bool = False,
        disable_react: bool = False,
        disable_stitch: bool = False,
        is_story: bool = False,
    ) -> dict[str, Any]:
        form_fields: dict[str, Any] = {
            "region": (region_override or account.region).upper(),
            "description": description,
            "music_id": music_id,
            "is_private": "1" if is_private else "0",
            "disable_comments": "1" if disable_comments else "0",
            "disable_duet": "1" if disable_duet else "0",
            "disable_react": "1" if disable_react else "0",
            "disable_stitch": "1" if disable_stitch else "0",
            "is_story": "1" if is_story else "0",
        }
        if description_mentions:
            form_fields["description_mentions"] = description_mentions

        video_bytes = media.path.read_bytes()
        files = [("video", (media.path.name, video_bytes, media.mime_type))]

        return await self._request(
            "POST",
            "/v1/post/create/video",
            account=account,
            data=form_fields,
            files=files,
        )

    async def create_photo_post(
        self,
        account: AccountRecord,
        media_list: list[PreparedMedia],
        *,
        description: str = "",
        description_mentions: list[str] | None = None,
        music_id: str = "",
        region_override: str | None = None,
        is_private: bool = False,
        disable_comments: bool = False,
        disable_duet: bool = False,
        disable_react: bool = False,
        disable_stitch: bool = False,
        is_story: bool = False,
    ) -> dict[str, Any]:
        form_fields: dict[str, Any] = {
            "region": (region_override or account.region).upper(),
            "description": description,
            "music_id": music_id,
            "is_private": "1" if is_private else "0",
            "disable_comments": "1" if disable_comments else "0",
            "disable_duet": "1" if disable_duet else "0",
            "disable_react": "1" if disable_react else "0",
            "disable_stitch": "1" if disable_stitch else "0",
            "is_story": "1" if is_story else "0",
        }
        if description_mentions:
            form_fields["description_mentions"] = description_mentions

        files = [
            ("photo", (m.path.name, m.path.read_bytes(), m.mime_type))
            for m in media_list
        ]

        return await self._request(
            "POST",
            "/v1/post/create/photo",
            account=account,
            data=form_fields,
            files=files,
        )

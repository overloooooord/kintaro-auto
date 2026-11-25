from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import shutil
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from kintaro_auto.appeals import (
    SORTED_FOLDER_SPECS,
    WATCH_POLL_SECONDS,
    AccountAuditResult,
    audit_account_restrictions,
    extract_cookie_dict,
    get_sorted_folders_summary,
    probe_proxy_geo,
    remove_account_restrictions,
    set_post_visibility,
    wait_and_appeal_settle,
)
from kintaro_auto.captions import prepare_description_and_mentions
from kintaro_auto.client import KintaroClient
from kintaro_auto.config import AppConfig, load_config
from kintaro_auto.models import (
    AccountRecord,
    CampaignConfig,
    UploadTask,
    apply_device_geo_profile,
)
from kintaro_auto.pipeline import AutomationPipeline
from kintaro_auto.platform_utils import configure_windows_runtime, find_chromium_executable
from kintaro_auto.presets import (
    delete_campaign_preset,
    delete_media_file,
    list_media_library,
    load_campaign_presets,
    save_campaign_preset,
    save_media_bytes,
)
from kintaro_auto.spy import (
    download_creative_to_media,
    fetch_post_comments_structured,
    generate_live_totp,
    run_top_comment_boost,
    spy_competitor_profile,
)
from kintaro_auto.storage import StateStore, normalize_proxy_url, parse_shop_account_line
from kintaro_auto.studio_ops import (
    AutoPilotScheduler,
    ScheduledJob,
    export_all_sessions_jsonl,
    export_all_sessions_zip,
    export_analytics_csv,
    export_sorted_folder_zip,
    import_raw_sessions_text,
    run_bulk_branding,
    run_engagement_boost,
)
from kintaro_auto.uniquifier import PHOTO_UNIQUIFIER_PRESETS, uniquify_photo_bytes

configure_windows_runtime()

logger = logging.getLogger("kintaro_auto")
STATIC_DIR = Path(__file__).resolve().parent / "static"


def _mask_proxy(px: str) -> str:
    if not px or "@" not in px:
        return px
    scheme_rest = px.split("://", 1)
    if len(scheme_rest) != 2:
        return px
    scheme, rest = scheme_rest
    creds, hostpart = rest.rsplit("@", 1)
    if ":" in creds:
        u, _ = creds.split(":", 1)
        return f"{scheme}://{u}:***@{hostpart}"
    return px


def _write_config_toml(path: Path, cfg: AppConfig) -> None:
    """Serialize editable AppConfig fields cleanly back to config.toml."""
    def _q(val: str) -> str:
        return json.dumps(val, ensure_ascii=False)

    def _b(val: bool) -> str:
        return "true" if val else "false"

    def _arr(items: list[str]) -> str:
        return "[" + ", ".join(_q(x) for x in items) + "]"

    lines = [
        f"state_dir = {_q(cfg.state_dir)}",
        f"max_parallel_accounts = {cfg.max_parallel_accounts}",
        "",
        "[api]",
        f"base_url = {_q(cfg.api.base_url)}",
        f"api_key = {_q(cfg.api.api_key)}",
        f"timeout_seconds = {cfg.api.timeout_seconds}",
        f"max_retries = {cfg.api.max_retries}",
        f"retry_backoff_seconds = {cfg.api.retry_backoff_seconds}",
        f"sync_totp_window = {_b(cfg.api.sync_totp_window)}",
        f"totp_min_remaining_seconds = {cfg.api.totp_min_remaining_seconds}",
        f"save_raw_responses = {_b(cfg.api.save_raw_responses)}",
        f"raw_logs_dir = {_q(cfg.api.raw_logs_dir)}",
        "",
        "[warmup]",
        f"enabled = {_b(cfg.warmup.enabled)}",
        f"min_session_age_minutes_before_first_post = {cfg.warmup.min_session_age_minutes_before_first_post}",
        f"min_hours_between_posts_per_account = {cfg.warmup.min_hours_between_posts_per_account}",
        f"max_posts_per_account_per_day = {cfg.warmup.max_posts_per_account_per_day}",
        f"feed_rounds_min = {cfg.warmup.feed_rounds_min}",
        f"feed_rounds_max = {cfg.warmup.feed_rounds_max}",
        f"views_per_round_min = {cfg.warmup.views_per_round_min}",
        f"views_per_round_max = {cfg.warmup.views_per_round_max}",
        f"playtime_ms_min = {cfg.warmup.playtime_ms_min}",
        f"playtime_ms_max = {cfg.warmup.playtime_ms_max}",
        f"like_probability = {cfg.warmup.like_probability}",
        f"read_comments_probability = {cfg.warmup.read_comments_probability}",
        f"follow_probability = {cfg.warmup.follow_probability}",
        f"action_delay_seconds_min = {cfg.warmup.action_delay_seconds_min}",
        f"action_delay_seconds_max = {cfg.warmup.action_delay_seconds_max}",
        f"pre_upload_pause_seconds_min = {cfg.warmup.pre_upload_pause_seconds_min}",
        f"pre_upload_pause_seconds_max = {cfg.warmup.pre_upload_pause_seconds_max}",
        "",
        "[uniquifier]",
        f"enabled = {_b(cfg.uniquifier.enabled)}",
        f"photo_mode = {_q(cfg.uniquifier.photo_mode)}",
        f"ffmpeg_bin = {_q(cfg.uniquifier.ffmpeg_bin)}",
        f"ffprobe_bin = {_q(cfg.uniquifier.ffprobe_bin)}",
        f"temp_dir = {_q(cfg.uniquifier.temp_dir)}",
        f"crop_ratio_min = {cfg.uniquifier.crop_ratio_min}",
        f"crop_ratio_max = {cfg.uniquifier.crop_ratio_max}",
        f"brightness_shift_max = {cfg.uniquifier.brightness_shift_max}",
        f"contrast_shift_max = {cfg.uniquifier.contrast_shift_max}",
        f"saturation_shift_max = {cfg.uniquifier.saturation_shift_max}",
        f"speed_jitter_max = {cfg.uniquifier.speed_jitter_max}",
        f"video_crf_min = {cfg.uniquifier.video_crf_min}",
        f"video_crf_max = {cfg.uniquifier.video_crf_max}",
        f"photo_jpeg_quality_min = {cfg.uniquifier.photo_jpeg_quality_min}",
        f"photo_jpeg_quality_max = {cfg.uniquifier.photo_jpeg_quality_max}",
        "",
        "[response_mapping]",
        f"device_keys = {_arr(cfg.response_mapping.device_keys)}",
        f"verify_keys = {_arr(cfg.response_mapping.verify_keys)}",
        f"feed_list_keys = {_arr(cfg.response_mapping.feed_list_keys)}",
        f"aweme_id_keys = {_arr(cfg.response_mapping.aweme_id_keys)}",
        f"play_count_keys = {_arr(cfg.response_mapping.play_count_keys)}",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


class DashboardHub:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path
        self.cfg: AppConfig = load_config(config_path)
        self.store = StateStore(self.cfg.state_dir)
        self.cache_file = Path(self.cfg.state_dir) / "audit_cache.json"
        self.audit_cache: dict[str, dict[str, Any]] = self._load_audit_cache()
        self.shadow_cache: list[dict[str, Any]] = []
        self.logs: list[dict[str, Any]] = []
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self.active_jobs: dict[str, dict[str, Any]] = {}
        self.watchdog_task: asyncio.Task[None] | None = None
        self.watchdog_enabled: bool = False
        self.watchdog_hide_rejected: bool = True
        self.watchdog_cycles: int = 0
        self.watchdog_next_run_ts: float | None = None

    def _load_audit_cache(self) -> dict[str, dict[str, Any]]:
        if self.cache_file.exists():
            try:
                data = json.loads(self.cache_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
        return {}

    def _save_audit_cache(self) -> None:
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            self.cache_file.write_text(
                json.dumps(self.audit_cache, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    def reload_env(self) -> None:
        self.cfg = load_config(self.config_path)
        self.store = StateStore(self.cfg.state_dir)

    def start_job(self, label: str, total: int = 1) -> str:
        jid = uuid.uuid4().hex[:8]
        self.active_jobs[jid] = {
            "id": jid,
            "label": label,
            "done": 0,
            "total": max(1, total),
            "started_at": datetime.now().strftime("%H:%M:%S"),
        }
        self.push_event(f"▶ {label}", "info", kind="jobs_updated")
        return jid

    def step_job(self, jid: str, delta: int = 1) -> None:
        if jid in self.active_jobs:
            self.active_jobs[jid]["done"] = min(
                self.active_jobs[jid]["total"], self.active_jobs[jid]["done"] + delta
            )
            self.push_event("", "info", kind="jobs_updated", silent_log=True)

    def end_job(self, jid: str) -> None:
        self.active_jobs.pop(jid, None)
        self.push_event("", "info", kind="jobs_updated", silent_log=True)

    def push_event(
        self,
        text: str,
        level: str = "info",
        kind: str = "log",
        extra: dict[str, Any] | None = None,
        silent_log: bool = False,
    ) -> None:
        entry: dict[str, Any] = {
            "id": uuid.uuid4().hex[:10],
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "kind": kind,
            "text": text,
        }
        if extra:
            entry["extra"] = extra
        if not silent_log and text:
            self.logs.append(entry)
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]
        for q in list(self.subscribers):
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass

    def default_proxy(self) -> str:
        accs = self.store.list_accounts()
        return accs[0].proxy if accs else ""

    def serialize_account(self, acc: AccountRecord) -> dict[str, Any]:
        cached = self.audit_cache.get(acc.account_id)
        return {
            "account_id": acc.account_id,
            "username": acc.username or acc.account_id,
            "email": acc.email,
            "region": acc.region,
            "auth_region": acc.auth_region or acc.region,
            "auth_method": acc.auth_method,
            "use_outlook_hub": acc.use_outlook_hub,
            "has_totp": bool(acc.totp_token),
            "authenticated": acc.authenticated and bool(acc.device),
            "login_cooldown_until": acc.login_cooldown_until,
            "proxy_masked": _mask_proxy(acc.proxy),
            "first_authenticated_at": acc.first_authenticated_at,
            "last_warmup_at": acc.last_warmup_at,
            "uploads_count": len(acc.upload_history),
            "upload_history": [r.model_dump(mode="json") for r in acc.upload_history[-10:]],
            "audit": cached,
        }

    async def run_audit_one(self, account_id: str) -> dict[str, Any]:
        acc = self.store.load_account(account_id)
        self.push_event(f"[{account_id}] Проверка прокси + HTTP-аудит IDC и Studio...", "info")
        aud: AccountAuditResult = await audit_account_restrictions(acc)
        self.store.save_account(acc)
        aud_dict = asdict(aud)
        aud_dict["audited_at"] = datetime.now().strftime("%H:%M:%S")
        self.audit_cache[account_id] = aud_dict
        self._save_audit_cache()
        self.push_event(
            f"[{account_id}] Готово: @{aud.uniq_id} | {aud.reg_country or '?'}→{aud.current_region or '?'} | "
            f"Proxy={aud.proxy_country or 'OK'} ({aud.proxy_ip or '—'}) | {aud.account_health.upper()} | "
            f"Постов={aud.total_posts} (views={aud.total_views})",
            "success" if aud.account_health == "eligible" else "warn",
            kind="audit_updated",
            extra={"account_id": account_id},
        )
        return aud_dict

    async def _watchdog_loop(self) -> None:
        self.push_event("Фоновый сторож (Watchdog) запущен: опрос по кукам раз в 95–165 сек.", "success", kind="watchdog")
        try:
            while self.watchdog_enabled:
                self.watchdog_cycles += 1
                targets = [a for a in self.store.list_accounts() if a.authenticated and a.device]
                jid = self.start_job(f"Watchdog #{self.watchdog_cycles}", total=max(1, len(targets)))
                try:
                    for acc in targets:
                        if not self.watchdog_enabled:
                            break
                        try:
                            summary = await remove_account_restrictions(
                                acc, hide_rejected=self.watchdog_hide_rejected
                            )
                            self.store.save_account(acc)
                            if summary.audit:
                                ad = asdict(summary.audit)
                                ad["audited_at"] = datetime.now().strftime("%H:%M:%S")
                                self.audit_cache[acc.account_id] = ad
                                self._save_audit_cache()
                            sent = summary.notice_appeals_verified + summary.post_appeals_verified
                            if sent > 0 or summary.posts_hidden > 0:
                                self.push_event(
                                    f"[{acc.account_id}] Снято флагов: {sent}, скрыто FAILED в Private: {summary.posts_hidden}",
                                    "success",
                                    kind="audit_updated",
                                )
                        except Exception as exc:
                            self.push_event(f"[{acc.account_id}] Ошибка сторожа: {exc}", "error")
                        finally:
                            self.step_job(jid)
                finally:
                    self.end_job(jid)

                if not self.watchdog_enabled:
                    break
                wait_s = random.uniform(*WATCH_POLL_SECONDS)
                self.watchdog_next_run_ts = time.time() + wait_s
                self.push_event(f"[Watchdog] Следующий проход через {wait_s:.0f} сек.", "info", kind="watchdog")
                await asyncio.sleep(wait_s)
        except asyncio.CancelledError:
            pass
        finally:
            self.watchdog_enabled = False
            self.watchdog_next_run_ts = None
            self.push_event("Фоновый сторож остановлен.", "warn", kind="watchdog")


# ── Pydantic Request Schemas ─────────────────────────────────────────────────

class LinePreviewRequest(BaseModel):
    lines_text: str
    proxies_text: str = ""
    region: str = "US"


class BatchImportRequest(BaseModel):
    lines_text: str
    proxies_text: str = ""
    proxy: str = ""
    region: str = "US"
    auto_login: bool = True
    auto_clean_posts: bool = False
    auto_appeal: bool = True
    hide_rejected: bool = True


class FolderImportRequest(BaseModel):
    folder_path: str
    proxy: str = ""
    region: str = "US"


class AppealRequest(BaseModel):
    reason: str = "Our content is original, authentic, and strictly follows all TikTok Community Guidelines."
    dry_run: bool = False
    appeal_studio_posts: bool = False
    settle: bool = False
    quiet_seconds: float = 60.0
    max_wait_seconds: float = 240.0
    hide_rejected: bool = True


class PostVisibilityRequest(BaseModel):
    private: bool = True


class GeoSyncRequest(BaseModel):
    region: str = "US"


class ProxyUpdateRequest(BaseModel):
    proxy: str


class ProfileUpdateRequest(BaseModel):
    nickname: str = ""
    bio: str = ""
    bio_url: str = ""


class BulkActionRequest(BaseModel):
    account_ids: list[str]
    action: str  # auth | audit | appeal | clean_posts | restart | warmup | sync_geo | set_proxy | delete
    region: str = "US"
    proxy: str = ""
    appeal_studio_posts: bool = False
    hide_rejected: bool = True
    reason: str = "Our content is original, authentic, and strictly follows all TikTok Community Guidelines."


class ProxyTestRequest(BaseModel):
    proxies_text: str


class SpintaxPreviewRequest(BaseModel):
    template: str
    mentions: list[str] = []


class WatchdogToggleRequest(BaseModel):
    enabled: bool
    hide_rejected: bool = True


class SpyRequest(BaseModel):
    username: str
    account_id: str = ""
    count: int = 18


class TotpRequest(BaseModel):
    secret: str


class SpyDownloadRequest(BaseModel):
    target_post: str
    account_id: str = ""


class CommentsListRequest(BaseModel):
    target_post: str
    account_id: str = ""
    count: int = 30


class CommentsBoostRequest(BaseModel):
    target_post: str
    seed_account_id: str = ""
    lead_comment_text: str = ""
    existing_cid: str = ""
    booster_account_ids: list[str] = []
    reply_templates: str = ""
    reply_prob_pct: int = 30
    threads: int = 4


class EngagementBoostRequest(BaseModel):
    account_ids: list[str] = []
    target_post: str = ""
    target_username: str = ""
    do_view: bool = True
    do_like: bool = True
    do_collect: bool = False
    do_repost: bool = False
    comments_text: str = ""
    threads: int = 4


class BrandingRequest(BaseModel):
    account_ids: list[str] = []
    avatar_data_url: str = ""
    uniquify_avatar: bool = True
    nickname_template: str = ""
    username_prefix: str = ""
    bio_template: str = ""
    bio_url: str = ""
    new_password: str = ""
    enable_totp: bool = False
    threads: int = 3


class RawSessionsImportRequest(BaseModel):
    raw_text: str
    default_proxy: str = ""
    default_region: str = "US"


class AutoPilotAddRequest(BaseModel):
    action: str  # "audit_all" | "appeal_all" | "shadow_check"
    interval_minutes: int = 30
    delay_minutes: int = 0
    note: str = ""


class ConfigUpdateRequest(BaseModel):
    api_key: str | None = None
    max_parallel_accounts: int | None = None
    warmup_enabled: bool | None = None
    min_session_age_minutes_before_first_post: float | None = None
    min_hours_between_posts_per_account: float | None = None
    max_posts_per_account_per_day: int | None = None
    uniquifier_enabled: bool | None = None


def _parse_proxy_pool(proxies_text: str, fallback_proxy: str) -> list[str]:
    pool = [
        normalize_proxy_url(ln.strip())
        for ln in (proxies_text or "").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not pool and fallback_proxy:
        pool = [normalize_proxy_url(fallback_proxy)]
    return pool


def create_web_app(config_path: Path | None = None) -> FastAPI:
    hub = DashboardHub(config_path)
    app = FastAPI(title="Kintaro Auto · TikTok Farm OS", version="3.0.0")
    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    class _HubLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            msg = record.getMessage()
            lvl = (
                "error"
                if record.levelno >= logging.ERROR
                else ("warn" if record.levelno >= logging.WARNING else "info")
            )
            hub.push_event(msg, lvl)

    lg = logging.getLogger("kintaro_auto")
    if not any(isinstance(h, _HubLogHandler) for h in lg.handlers):
        h = _HubLogHandler()
        h.setLevel(logging.INFO)
        lg.addHandler(h)
        lg.setLevel(logging.INFO)

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        hub.reload_env()
        accs = [hub.serialize_account(a) for a in hub.store.list_accounts()]
        return {
            "accounts": accs,
            "default_proxy_masked": _mask_proxy(hub.default_proxy()),
            "has_default_proxy": bool(hub.default_proxy()),
            "active_jobs": list(hub.active_jobs.values()),
            "shadows": hub.shadow_cache,
            "watchdog": {
                "enabled": hub.watchdog_enabled,
                "hide_rejected": hub.watchdog_hide_rejected,
                "cycles": hub.watchdog_cycles,
                "next_in_sec": max(0, int(hub.watchdog_next_run_ts - time.time()))
                if hub.watchdog_next_run_ts
                else None,
            },
            "logs": hub.logs[-120:],
        }

    @app.get("/api/events")
    async def sse_events() -> StreamingResponse:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=250)
        hub.subscribers.add(q)

        async def _gen():
            try:
                yield f"data: {json.dumps({'kind': 'connected', 'time': datetime.now().strftime('%H:%M:%S'), 'level': 'info', 'text': 'Подключено к live-телеметрии Farm OS'})}\n\n"
                while True:
                    try:
                        item = await asyncio.wait_for(q.get(), timeout=15.0)
                        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                hub.subscribers.discard(q)

        return StreamingResponse(_gen(), media_type="text/event-stream")

    @app.post("/api/audit-all")
    async def audit_all() -> dict[str, Any]:
        targets = [a for a in hub.store.list_accounts() if a.authenticated and a.device]
        jid = hub.start_job(f"HTTP-аудит ({len(targets)} акк.)", total=max(1, len(targets)))
        sem = asyncio.Semaphore(3)

        async def _one(aid: str) -> None:
            async with sem:
                try:
                    await hub.run_audit_one(aid)
                except Exception as exc:
                    hub.push_event(f"[{aid}] Ошибка аудита: {exc}", "error")
                finally:
                    hub.step_job(jid)

        try:
            await asyncio.gather(*(_one(a.account_id) for a in targets))
        finally:
            hub.end_job(jid)
        return {"ok": True, "audited": len(targets)}

    @app.post("/api/accounts/{account_id}/audit")
    async def audit_one(account_id: str) -> dict[str, Any]:
        jid = hub.start_job(f"Аудит @{account_id}", total=1)
        try:
            res = await hub.run_audit_one(account_id)
            return {"ok": True, "audit": res}
        except Exception as exc:
            hub.push_event(f"[{account_id}] Ошибка аудита: {exc}", "error")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            hub.end_job(jid)

    @app.post("/api/accounts/{account_id}/appeal")
    async def appeal_one(account_id: str, req: AppealRequest) -> dict[str, Any]:
        jid = hub.start_job(
            f"{'Ожидание волны (0..10+) и снятие' if req.settle else 'Снятие огранов'} @{account_id}",
            total=1,
        )
        try:
            acc = hub.store.load_account(account_id)
            hub.push_event(
                f"[{account_id}] Запуск снятия огранов Account updates"
                + (" (режим ожидания всей волны до 240с)..." if req.settle else "..."),
                "info",
            )
            if req.settle and not req.dry_run:
                summary = await wait_and_appeal_settle(
                    acc,
                    appeal_text=req.reason,
                    appeal_studio_posts=req.appeal_studio_posts,
                    hide_rejected=req.hide_rejected,
                    quiet_seconds=req.quiet_seconds,
                    max_wait_seconds=req.max_wait_seconds,
                )
            else:
                summary = await remove_account_restrictions(
                    acc,
                    appeal_text=req.reason,
                    dry_run=req.dry_run,
                    appeal_studio_posts=req.appeal_studio_posts,
                    hide_rejected=req.hide_rejected,
                )
            hub.store.save_account(acc)
            if summary.audit:
                ad = asdict(summary.audit)
                ad["audited_at"] = datetime.now().strftime("%H:%M:%S")
                hub.audit_cache[account_id] = ad
                hub._save_audit_cache()
            hub.push_event(
                f"[{account_id}] Готово: снято в Account updates={summary.notice_appeals_verified}, "
                f"постов Studio={summary.post_appeals_verified}, скрыто в Private={summary.posts_hidden}",
                "success",
                kind="audit_updated",
            )
            return {"ok": True, "summary": asdict(summary)}
        except Exception as exc:
            hub.push_event(f"[{account_id}] Ошибка апелляции: {exc}", "error")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            hub.end_job(jid)

    @app.post("/api/appeal-all")
    async def appeal_all(req: AppealRequest) -> dict[str, Any]:
        targets = [a for a in hub.store.list_accounts() if a.authenticated and a.device]
        jid = hub.start_job(f"Снятие огранов ({len(targets)} акк.)", total=max(1, len(targets)))
        out = []
        try:
            for acc in targets:
                try:
                    if req.settle and not req.dry_run:
                        s = await wait_and_appeal_settle(
                            acc,
                            appeal_text=req.reason,
                            appeal_studio_posts=req.appeal_studio_posts,
                            hide_rejected=req.hide_rejected,
                            quiet_seconds=req.quiet_seconds,
                            max_wait_seconds=req.max_wait_seconds,
                        )
                    else:
                        s = await remove_account_restrictions(
                            acc,
                            appeal_text=req.reason,
                            dry_run=req.dry_run,
                            appeal_studio_posts=req.appeal_studio_posts,
                            hide_rejected=req.hide_rejected,
                        )
                    hub.store.save_account(acc)
                    if s.audit:
                        ad = asdict(s.audit)
                        ad["audited_at"] = datetime.now().strftime("%H:%M:%S")
                        hub.audit_cache[acc.account_id] = ad
                        hub._save_audit_cache()
                    out.append(asdict(s))
                except Exception as exc:
                    hub.push_event(f"[{acc.account_id}] Ошибка апелляции: {exc}", "error")
                finally:
                    hub.step_job(jid)
        finally:
            hub.end_job(jid)
        hub.push_event(f"Массовое снятие огранов завершено ({len(out)}/{len(targets)} акк.)", "success", kind="audit_updated")
        return {"ok": True, "results": out}

    @app.post("/api/bulk-action")
    async def bulk_action(req: BulkActionRequest) -> dict[str, Any]:
        ids = [x for x in req.account_ids if x]
        if not ids:
            raise HTTPException(status_code=400, detail="Не выбрано ни одного аккаунта.")

        async def _run_bulk() -> None:
            jid = hub.start_job(f"Пакетное действие `{req.action}` ({len(ids)} акк.)", total=len(ids))
            try:
                if req.action == "delete":
                    for aid in ids:
                        p = hub.store.account_path(aid)
                        if p.exists():
                            p.unlink()
                        hub.audit_cache.pop(aid, None)
                        hub.step_job(jid)
                    hub._save_audit_cache()
                    hub.push_event(f"Удалено аккаунтов: {len(ids)}", "warn", kind="audit_updated")
                    return

                if req.action == "sync_geo":
                    reg = req.region.strip().upper()
                    for aid in ids:
                        acc = hub.store.load_account(aid)
                        acc.region = reg
                        if acc.device is not None:
                            acc.device = apply_device_geo_profile(acc.device, reg)
                        hub.store.save_account(acc)
                        hub.step_job(jid)
                    hub.push_event(f"SIM/GEO синхронизирован на {reg} для {len(ids)} акк.", "success", kind="audit_updated")
                    return

                if req.action == "set_proxy":
                    px = normalize_proxy_url(req.proxy)
                    if not px:
                        hub.push_event("Пустой прокси для массового назначения!", "error")
                        return
                    for aid in ids:
                        acc = hub.store.load_account(aid)
                        acc.proxy = px
                        hub.store.save_account(acc)
                        hub.step_job(jid)
                    hub.push_event(f"Новый прокси назначен на {len(ids)} акк.", "success", kind="audit_updated")
                    return

                if req.action == "auth":
                    async with KintaroClient(hub.cfg) as client:
                        pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                        for aid in ids:
                            try:
                                acc = hub.store.load_account(aid)
                                if not acc.authenticated or not acc.device:
                                    hub.push_event(f"[{aid}] Авторизация ({'Outlook Hub' if acc.use_outlook_hub else 'TOTP/Pass'})...", "info")
                                    acc = await pipeline.ensure_authenticated(acc)
                                    hub.push_event(f"[{aid}] Вход выполнен успешно!", "success", kind="audit_updated")
                            except Exception as exc:
                                hub.push_event(f"[{aid}] Ошибка входа: {exc}", "error")
                            finally:
                                hub.step_job(jid)
                    hub.push_event("Пакетная авторизация завершена.", "success", kind="audit_updated")
                    return

                if req.action == "clean_posts":
                    async with KintaroClient(hub.cfg) as client:
                        pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                        for aid in ids:
                            try:
                                acc = hub.store.load_account(aid)
                                cnt = await pipeline.delete_all_account_posts(acc)
                                if aid in hub.audit_cache:
                                    hub.audit_cache[aid]["post_penalties"] = []
                                    hub.audit_cache[aid]["total_posts"] = 0
                                    hub.audit_cache[aid]["total_views"] = 0
                                    hub._save_audit_cache()
                                hub.push_event(f"[{aid}] 🗑 Удалено старых постов: {cnt}", "success", kind="audit_updated")
                            except Exception as exc:
                                hub.push_event(f"[{aid}] Ошибка очистки постов: {exc}", "error")
                            finally:
                                hub.step_job(jid)
                    hub.push_event("Пакетная очистка постов завершена.", "success", kind="audit_updated")
                    return

                if req.action == "restart":
                    async with KintaroClient(hub.cfg) as client:
                        pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                        for aid in ids:
                            try:
                                acc = hub.store.load_account(aid)
                                await pipeline.restart_tiktok_app(acc, target_region=req.region)
                                hub.push_event(f"[{aid}] 🔄 Приложение TikTok перезапущено", "success", kind="audit_updated")
                            except Exception as exc:
                                hub.push_event(f"[{aid}] Ошибка рестарта: {exc}", "error")
                            finally:
                                hub.step_job(jid)
                    hub.push_event("Пакетный рестарт завершён.", "success", kind="audit_updated")
                    return

                if req.action == "audit":
                    for aid in ids:
                        try:
                            await hub.run_audit_one(aid)
                        except Exception as exc:
                            hub.push_event(f"[{aid}] Ошибка аудита: {exc}", "error")
                        finally:
                            hub.step_job(jid)
                    return

                if req.action == "appeal":
                    for aid in ids:
                        try:
                            acc = hub.store.load_account(aid)
                            s = await remove_account_restrictions(
                                acc,
                                appeal_text=req.reason,
                                appeal_studio_posts=req.appeal_studio_posts,
                                hide_rejected=req.hide_rejected,
                            )
                            hub.store.save_account(acc)
                            if s.audit:
                                ad = asdict(s.audit)
                                ad["audited_at"] = datetime.now().strftime("%H:%M:%S")
                                hub.audit_cache[aid] = ad
                                hub._save_audit_cache()
                        except Exception as exc:
                            hub.push_event(f"[{aid}] Ошибка апелляции: {exc}", "error")
                        finally:
                            hub.step_job(jid)
                    hub.push_event("Пакетное снятие огранов завершено.", "success", kind="audit_updated")
                    return

                if req.action == "warmup":
                    async with KintaroClient(hub.cfg) as client:
                        pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                        for aid in ids:
                            try:
                                acc = hub.store.load_account(aid)
                                st = await pipeline.run_warmup(acc)
                                hub.push_event(f"[{aid}] Прогрев готов: views={st['views']}, likes={st['likes']}", "success")
                            except Exception as exc:
                                hub.push_event(f"[{aid}] Ошибка прогрева: {exc}", "error")
                            finally:
                                hub.step_job(jid)
                    hub.push_event("Пакетный прогрев завершён.", "success", kind="audit_updated")
            finally:
                hub.end_job(jid)

        asyncio.create_task(_run_bulk())
        return {"ok": True, "queued": len(ids)}

    @app.post("/api/accounts/{account_id}/auth")
    async def auth_account(account_id: str) -> dict[str, Any]:
        jid = hub.start_job(f"Авторизация @{account_id}", total=1)
        try:
            acc = hub.store.load_account(account_id)
            async with KintaroClient(hub.cfg) as client:
                pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                acc = await pipeline.ensure_authenticated(acc)
            hub.push_event(f"[{account_id}] Сессия активна! Запускаю первичный аудит...", "success")
            await hub.run_audit_one(acc.account_id)
            return {"ok": True, "account": hub.serialize_account(acc)}
        except Exception as exc:
            hub.push_event(f"[{account_id}] Ошибка входа: {exc}", "error")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            hub.end_job(jid)

    @app.post("/api/accounts/{account_id}/sync-geo")
    async def sync_geo(account_id: str, req: GeoSyncRequest) -> dict[str, Any]:
        acc = hub.store.load_account(account_id)
        reg = req.region.strip().upper()
        acc.region = reg
        if acc.device is not None:
            acc.device = apply_device_geo_profile(acc.device, reg)
        hub.store.save_account(acc)
        hub.push_event(f"[{account_id}] SIM / Locale / Timezone внутри x-device переключены на {reg}.", "success", kind="audit_updated")
        return {"ok": True, "account": hub.serialize_account(acc)}

    @app.post("/api/accounts/{account_id}/proxy")
    async def update_account_proxy(account_id: str, req: ProxyUpdateRequest) -> dict[str, Any]:
        px = normalize_proxy_url(req.proxy)
        if not px:
            raise HTTPException(status_code=400, detail="Укажи корректный прокси.")
        t0 = time.perf_counter()
        try:
            ip, cc = await probe_proxy_geo(px)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Прокси не прошёл проверку Cloudflare trace: {exc}") from exc
        ms = int((time.perf_counter() - t0) * 1000)
        acc = hub.store.load_account(account_id)
        acc.proxy = px
        hub.store.save_account(acc)
        hub.push_event(f"[{account_id}] Прокси обновлён и проверен: {cc} · {ip} ({ms} мс)", "success", kind="audit_updated")
        return {"ok": True, "ip": ip, "country": cc, "latency_ms": ms, "proxy_masked": _mask_proxy(px)}

    @app.post("/api/accounts/{account_id}/profile")
    async def update_profile(account_id: str, req: ProfileUpdateRequest) -> dict[str, Any]:
        acc = hub.store.load_account(account_id)
        updated: list[str] = []
        try:
            async with KintaroClient(hub.cfg) as client:
                if req.nickname.strip():
                    await client.set_nickname(acc, req.nickname.strip())
                    updated.append(f"Nickname='{req.nickname.strip()}'")
                if req.bio.strip():
                    await client.set_bio(acc, req.bio.strip())
                    updated.append("Bio")
                if req.bio_url.strip():
                    await client.set_bio_url(acc, req.bio_url.strip())
                    updated.append("Link")
            hub.store.save_account(acc)
            msg = f"Профиль обновлён: {', '.join(updated)}" if updated else "Нет изменений"
            hub.push_event(f"[{account_id}] {msg}", "success", kind="audit_updated")
            return {"ok": True, "message": msg}
        except Exception as exc:
            hub.push_event(f"[{account_id}] Ошибка обновления профиля: {exc}", "error")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/accounts/{account_id}/outlook-otp")
    async def read_outlook_otp(account_id: str) -> dict[str, Any]:
        acc = hub.store.load_account(account_id)
        if not acc.email or not acc.mail_password:
            raise HTTPException(status_code=400, detail="У аккаунта не сохранены email и пароль почты Outlook.")
        from kintaro_auto.outlook_hub import prepare_outlook_hub_mailbox, wait_for_outlook_hub_code

        jid = hub.start_job(f"Чтение OTP из {acc.email}", total=1)
        try:
            tok, old_ids = await asyncio.to_thread(
                prepare_outlook_hub_mailbox,
                outlook_email=acc.email,
                outlook_login=acc.mail_login or acc.email,
                outlook_password=acc.mail_password,
                recovery_email=acc.recovery_email,
                recovery_password=acc.recovery_password,
                hotmail_totp=acc.mail_totp,
            )
            # Fetch latest TikTok OTP received within last 10 minutes
            code = await asyncio.to_thread(
                wait_for_outlook_hub_code,
                outlook_email=acc.email,
                outlook_login=acc.mail_login or acc.email,
                outlook_password=acc.mail_password,
                recovery_email=acc.recovery_email,
                recovery_password=acc.recovery_password,
                hotmail_totp=acc.mail_totp,
                min_timestamp=time.time() - 600.0,
                pre_access_token=tok,
                known_old_ids=set(),
                timeout_sec=12,
                poll_interval_sec=1.0,
            )
            hub.push_event(f"[{account_id}] Последний OTP из {acc.email}: {code}", "success")
            return {"ok": True, "code": code, "inbox_count": len(old_ids)}
        except Exception as exc:
            hub.push_event(f"[{account_id}] Outlook OTP: {exc}", "warn")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            hub.end_job(jid)

    @app.get("/api/accounts/{account_id}/export")
    async def export_session(account_id: str, mode: str = "cookies") -> JSONResponse:
        acc = hub.store.load_account(account_id)
        if mode == "cookies":
            data = extract_cookie_dict(acc)
            fname = f"{account_id}_cookies.json"
        else:
            data = acc.model_dump(mode="json")
            fname = f"{account_id}_session.json"
        return JSONResponse(
            content=data,
            headers={"Content-Disposition": f'attachment; filename="{fname}"'},
        )

    @app.post("/api/accounts/{account_id}/warmup")
    async def warmup_one(account_id: str) -> dict[str, Any]:
        jid = hub.start_job(f"Прогрев @{account_id}", total=1)
        try:
            acc = hub.store.load_account(account_id)
            async with KintaroClient(hub.cfg) as client:
                pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                stats = await pipeline.run_warmup(acc)
            hub.push_event(
                f"[{account_id}] Прогрев завершён: просмотров={stats['views']}, лайков={stats['likes']}",
                "success",
                kind="audit_updated",
            )
            return {"ok": True, "stats": stats}
        except Exception as exc:
            hub.push_event(f"[{account_id}] Ошибка прогрева: {exc}", "error")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            hub.end_job(jid)

    @app.post("/api/accounts/{account_id}/posts/{item_id}/visibility")
    async def change_visibility(account_id: str, item_id: str, req: PostVisibilityRequest) -> dict[str, Any]:
        acc = hub.store.load_account(account_id)
        ok, msg = await set_post_visibility(acc, item_id, private=req.private)
        hub.store.save_account(acc)
        hub.push_event(f"[{account_id}] {msg}", "success" if ok else "warn")
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"ok": True, "message": msg}

    @app.delete("/api/accounts/{account_id}/posts/{item_id}")
    async def delete_single_post_api(account_id: str, item_id: str) -> dict[str, Any]:
        acc = hub.store.load_account(account_id)
        try:
            async with KintaroClient(hub.cfg) as client:
                pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                ok, msg = await pipeline.delete_single_post(acc, item_id)
        except Exception as exc:
            hub.push_event(f"[{account_id}] Ошибка удаления поста {item_id}: {exc}", "error")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not ok:
            hub.push_event(f"[{account_id}] {msg}", "warn")
            raise HTTPException(status_code=400, detail=msg)
        if account_id in hub.audit_cache:
            cached_posts = [
                p for p in (hub.audit_cache[account_id].get("post_penalties") or [])
                if str(p.get("item_id")) != str(item_id)
            ]
            hub.audit_cache[account_id]["post_penalties"] = cached_posts
            hub.audit_cache[account_id]["total_posts"] = len(cached_posts)
            hub.audit_cache[account_id]["total_views"] = sum(int(p.get("play_count") or 0) for p in cached_posts)
            hub._save_audit_cache()
        hub.push_event(f"[{account_id}] 🗑 {msg}", "success", kind="audit_updated")
        return {"ok": True, "message": msg}

    @app.delete("/api/accounts/{account_id}")
    async def delete_account(account_id: str) -> dict[str, Any]:
        p = hub.store.account_path(account_id)
        if p.exists():
            p.unlink()
        hub.audit_cache.pop(account_id, None)
        hub._save_audit_cache()
        hub.push_event(f"Аккаунт [{account_id}] удалён из локального хранилища.", "warn", kind="audit_updated")
        return {"ok": True}

    @app.post("/api/preview-lines")
    async def preview_lines(req: LinePreviewRequest) -> dict[str, Any]:
        pool = _parse_proxy_pool(req.proxies_text, hub.default_proxy())
        rows = []
        raw_lines = [ln.strip() for ln in req.lines_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        for idx, ln in enumerate(raw_lines):
            assigned_px = pool[idx % len(pool)] if pool else "http://0.0.0.0:1"
            try:
                acc = parse_shop_account_line(ln, proxy=assigned_px, region=req.region)
                rows.append({
                    "ok": True,
                    "account_id": acc.account_id,
                    "format": "Outlook Hub :" if acc.use_outlook_hub else "TikTok TOTP |",
                    "email": acc.email or "—",
                    "has_2fa": bool(acc.totp_token or acc.mail_totp),
                    "proxy_masked": _mask_proxy(acc.proxy) if pool or "|" in ln else "⚠ Нет прокси",
                })
            except Exception as exc:
                rows.append({"ok": False, "raw": ln[:48], "error": str(exc)})
        return {"ok": True, "rows": rows, "valid_count": sum(1 for r in rows if r["ok"])}

    @app.post("/api/import-batch")
    async def import_batch(req: BatchImportRequest) -> dict[str, Any]:
        pool = _parse_proxy_pool(req.proxies_text or req.proxy, hub.default_proxy())
        lines = [ln.strip() for ln in req.lines_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if not lines:
            raise HTTPException(status_code=400, detail="Список строк пуст.")

        imported_ids: list[str] = []
        for idx, ln in enumerate(lines):
            px_for_row = pool[idx % len(pool)] if pool else ""
            try:
                acc = parse_shop_account_line(ln, proxy=px_for_row, region=req.region)
            except Exception as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            if hub.store.account_path(acc.account_id).exists():
                old = hub.store.load_account(acc.account_id)
                if old.device is not None:
                    acc.device = old.device
                    acc.authenticated = old.authenticated
                    acc.first_authenticated_at = old.first_authenticated_at
                    acc.upload_history = old.upload_history
            hub.store.save_account(acc)
            imported_ids.append(acc.account_id)

        hub.push_event(f"Импортировано аккаунтов: {len(imported_ids)}", "success", kind="audit_updated")

        async def _bg_pipeline() -> None:
            jid = hub.start_job(f"Авто-вход и разбан ({len(imported_ids)} акк.)", total=len(imported_ids))
            try:
                async with KintaroClient(hub.cfg) as client:
                    pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                    for aid in imported_ids:
                        acc = hub.store.load_account(aid)
                        if req.auto_login and (not acc.authenticated or not acc.device):
                            try:
                                hub.push_event(f"[{aid}] Вход ({'Outlook Hub' if acc.use_outlook_hub else 'TOTP/Pass'})...", "info")
                                acc = await pipeline.ensure_authenticated(acc)
                                hub.push_event(f"[{aid}] Вход выполнен успешно!", "success", kind="audit_updated")
                            except Exception as exc:
                                hub.push_event(f"[{aid}] Ошибка входа: {exc}", "error")
                                hub.step_job(jid)
                                continue
                        if req.auto_clean_posts and acc.authenticated and acc.device:
                            try:
                                del_cnt = await pipeline.delete_all_account_posts(acc)
                                hub.push_event(f"[{aid}] 🗑 Удалено старых постов: {del_cnt}", "success", kind="audit_updated")
                            except Exception as exc:
                                hub.push_event(f"[{aid}] Ошибка очистки постов: {exc}", "error")
                        if req.auto_appeal and acc.authenticated and acc.device:
                            try:
                                s = await remove_account_restrictions(acc, hide_rejected=req.hide_rejected)
                                hub.store.save_account(acc)
                                if s.audit:
                                    ad = asdict(s.audit)
                                    ad["audited_at"] = datetime.now().strftime("%H:%M:%S")
                                    hub.audit_cache[aid] = ad
                                    hub._save_audit_cache()
                                hub.push_event(f"[{aid}] Проверка и снятие огранов завершены!", "success", kind="audit_updated")
                            except Exception as exc:
                                hub.push_event(f"[{aid}] Ошибка снятия огранов: {exc}", "error")
                        hub.step_job(jid)
            finally:
                hub.end_job(jid)

        if req.auto_login or req.auto_clean_posts or req.auto_appeal:
            asyncio.create_task(_bg_pipeline())

        return {"ok": True, "imported": imported_ids}

    @app.post("/api/import-folder")
    async def import_folder(req: FolderImportRequest) -> dict[str, Any]:
        sdir = Path(req.folder_path.strip()).expanduser()
        if not sdir.is_dir():
            raise HTTPException(status_code=400, detail=f"Папка не найдена: {sdir}")
        fallback_px = normalize_proxy_url(req.proxy.strip()) if req.proxy.strip() else hub.default_proxy()
        reg = req.region.strip().upper()
        imported: list[str] = []

        for fp in sorted(sdir.glob("*.json")):
            try:
                raw = json.loads(fp.read_text(encoding="utf-8"))
                dev = raw.get("device") or (json.loads(raw["x_device"]) if raw.get("x_device") else None)
                if not dev:
                    continue
                uname = raw.get("username") or fp.stem
                px = fallback_px or raw.get("proxy", "")
                if not px:
                    continue
                acc = AccountRecord(
                    account_id=uname,
                    proxy=px,
                    region=reg,
                    auth_region="DE",
                    auth_method="device_only",
                    username=uname,
                    password=raw.get("tiktok_password") or "",
                    email=raw.get("email") or "",
                    totp_token=raw.get("totp_token") or "",
                    device=apply_device_geo_profile(dev, reg),
                    authenticated=True,
                    first_authenticated_at=raw.get("logged_in_at"),
                )
                hub.store.save_account(acc)
                imported.append(uname)
            except Exception:
                continue

        for cfile in sorted(sdir.glob("*/cookies.json")):
            try:
                cks = json.loads(cfile.read_text(encoding="utf-8"))
                if not isinstance(cks, dict) or not (cks.get("sessionid") or cks.get("sid_tt")):
                    continue
                uname = cfile.parent.name
                if not fallback_px:
                    continue
                acc = AccountRecord(
                    account_id=uname,
                    proxy=fallback_px,
                    region=reg,
                    auth_region="DE",
                    auth_method="device_only",
                    username=uname,
                    device={"cookies": cks},
                    authenticated=True,
                )
                hub.store.save_account(acc)
                imported.append(uname)
            except Exception:
                continue

        hub.push_event(f"Импортировано сессий из папки {sdir.name}: {len(imported)}", "success", kind="audit_updated")
        return {"ok": True, "imported": imported}

    @app.post("/api/spintax-preview")
    async def spintax_preview(req: SpintaxPreviewRequest) -> dict[str, Any]:
        variants = []
        for seed in range(4):
            desc, mentions = prepare_description_and_mentions(
                req.template, explicit_mentions=req.mentions, rng=random.Random(seed + int(time.time()))
            )
            variants.append({"text": desc, "mentions": mentions})
        return {"ok": True, "variants": variants}

    @app.post("/api/upload")
    async def upload_media(
        files: list[UploadFile] = File(...),
        account_ids: str = Form(""),
        description: str = Form(""),
        music_search: str = Form(""),
        skip_warmup: bool = Form(False),
        force_cooldown: bool = Form(True),
        delete_old_posts: bool = Form(False),
        appeal_after_upload: bool = Form(False),
        settle_after_upload: bool = Form(True),
    ) -> dict[str, Any]:
        if not files:
            raise HTTPException(status_code=400, detail="Файлы не выбраны.")

        tmp_dir = Path(tempfile.mkdtemp(prefix="kintaro_web_up_"))
        saved_paths: list[Path] = []
        for uf in files:
            suffix = Path(uf.filename or "media.jpg").suffix.lower() or ".jpg"
            dest = tmp_dir / f"{uuid.uuid4().hex[:8]}{suffix}"
            dest.write_bytes(await uf.read())
            saved_paths.append(dest)

        first_ext = saved_paths[0].suffix.lower()
        is_video = first_ext == ".mp4"
        targets = [x.strip() for x in account_ids.split(",") if x.strip()] or [None]

        results = []
        jid = hub.start_job(f"Анти-тень залив ({len(targets)} акк.)", total=len(targets))
        try:
            async with KintaroClient(hub.cfg) as client:
                pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                for tid in targets:
                    task = UploadTask(
                        task_id=f"web_{uuid.uuid4().hex[:8]}",
                        account_id=tid,
                        media_type="video" if is_video else "photo",
                        video_path=str(saved_paths[0]) if is_video else None,
                        photo_paths=[] if is_video else [str(p) for p in saved_paths],
                        description=description,
                        music_search_keyword=music_search.strip() or None,
                    )
                    try:
                        acc, rec = await pipeline.execute_upload_task(
                            task,
                            force_cooldown=force_cooldown,
                            skip_warmup=skip_warmup,
                            delete_old_posts=delete_old_posts,
                            appeal_after_upload=appeal_after_upload,
                            settle_after_upload=settle_after_upload,
                        )
                        hub.push_event(
                            f"[{acc.account_id}] Успешно залит пост! aweme_id={rec.aweme_id}",
                            "success",
                            kind="audit_updated",
                        )
                        results.append({"ok": True, "account_id": acc.account_id, "aweme_id": rec.aweme_id})
                    except Exception as exc:
                        hub.push_event(f"[{tid or 'auto'}] Ошибка залива: {exc}", "error")
                        results.append({"ok": False, "account_id": tid or "auto", "error": str(exc)})
                    finally:
                        hub.step_job(jid)
        finally:
            hub.end_job(jid)
        return {"ok": any(r["ok"] for r in results), "results": results}

    @app.post("/api/shadows/check")
    async def check_shadows_api(threshold_minutes: float = 45.0) -> dict[str, Any]:
        targets = [a for a in hub.store.list_accounts() if a.authenticated and a.upload_history]
        jid = hub.start_job(f"Радар теней ({len(targets)} акк.)", total=max(1, len(targets)))
        reports_out: list[dict[str, Any]] = []
        try:
            async with KintaroClient(hub.cfg) as client:
                pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                for acc in targets:
                    try:
                        reps = await pipeline.check_account_shadows(
                            acc, zero_view_threshold_minutes=threshold_minutes
                        )
                        for r in reps:
                            reports_out.append(r.model_dump(mode="json"))
                    except Exception as exc:
                        hub.push_event(f"[{acc.account_id}] Ошибка радара теней: {exc}", "warn")
                    finally:
                        hub.step_job(jid)
        finally:
            hub.end_job(jid)
        hub.shadow_cache = reports_out
        hub.push_event(f"Радар теней завершён: проверено постов из истории {len(reports_out)}", "success", kind="audit_updated")
        return {"ok": True, "reports": reports_out}

    @app.post("/api/proxies/test")
    async def test_proxies(req: ProxyTestRequest) -> dict[str, Any]:
        raw_list = [ln.strip() for ln in req.proxies_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if not raw_list:
            raise HTTPException(status_code=400, detail="Список прокси пуст.")
        sem = asyncio.Semaphore(6)

        async def _probe(raw: str) -> dict[str, Any]:
            norm = normalize_proxy_url(raw)
            async with sem:
                t0 = time.perf_counter()
                try:
                    ip, cc = await probe_proxy_geo(norm)
                    return {
                        "ok": True,
                        "proxy_masked": _mask_proxy(norm),
                        "ip": ip,
                        "country": cc,
                        "latency_ms": int((time.perf_counter() - t0) * 1000),
                    }
                except Exception as exc:
                    return {"ok": False, "proxy_masked": _mask_proxy(norm), "error": str(exc)[:110]}

        results = await asyncio.gather(*(_probe(x) for x in raw_list))
        return {"ok": True, "results": results}

    @app.get("/api/config")
    async def get_config_api() -> dict[str, Any]:
        hub.reload_env()
        k = hub.cfg.api.api_key
        masked = (k[:4] + "••••" + k[-4:]) if len(k) > 8 else ("••••" if k else "")
        return {
            "api_key_masked": masked,
            "has_api_key": bool(k),
            "max_parallel_accounts": hub.cfg.max_parallel_accounts,
            "warmup_enabled": hub.cfg.warmup.enabled,
            "min_session_age_minutes_before_first_post": hub.cfg.warmup.min_session_age_minutes_before_first_post,
            "min_hours_between_posts_per_account": hub.cfg.warmup.min_hours_between_posts_per_account,
            "max_posts_per_account_per_day": hub.cfg.warmup.max_posts_per_account_per_day,
            "uniquifier_enabled": hub.cfg.uniquifier.enabled,
        }

    @app.post("/api/config")
    async def save_config_api(req: ConfigUpdateRequest) -> dict[str, Any]:
        hub.reload_env()
        cfg = hub.cfg
        if req.api_key is not None and req.api_key.strip():
            cfg.api.api_key = req.api_key.strip()
        if req.max_parallel_accounts is not None:
            cfg.max_parallel_accounts = max(1, min(16, req.max_parallel_accounts))
        if req.warmup_enabled is not None:
            cfg.warmup.enabled = req.warmup_enabled
        if req.min_session_age_minutes_before_first_post is not None:
            cfg.warmup.min_session_age_minutes_before_first_post = max(0.0, req.min_session_age_minutes_before_first_post)
        if req.min_hours_between_posts_per_account is not None:
            cfg.warmup.min_hours_between_posts_per_account = max(0.0, req.min_hours_between_posts_per_account)
        if req.max_posts_per_account_per_day is not None:
            cfg.warmup.max_posts_per_account_per_day = max(1, req.max_posts_per_account_per_day)
        if req.uniquifier_enabled is not None:
            cfg.uniquifier.enabled = req.uniquifier_enabled

        target_path = hub.config_path or Path("config.toml")
        _write_config_toml(target_path, cfg)
        hub.reload_env()
        hub.push_event(f"Настройки сохранены в {target_path.name}.", "success")
        return {"ok": True}

    @app.post("/api/watchdog")
    async def toggle_watchdog(req: WatchdogToggleRequest) -> dict[str, Any]:
        hub.watchdog_hide_rejected = req.hide_rejected
        if req.enabled and not hub.watchdog_enabled:
            hub.watchdog_enabled = True
            hub.watchdog_task = asyncio.create_task(hub._watchdog_loop())
        elif not req.enabled and hub.watchdog_enabled:
            hub.watchdog_enabled = False
            if hub.watchdog_task:
                hub.watchdog_task.cancel()
        return {"ok": True, "enabled": hub.watchdog_enabled}

    # ── Merged swegby Studio Endpoints (Campaign v4, Presets, Uniquifier Preview, Restart, Clean, Spy, TOTP) ──

    @app.get("/api/presets")
    async def get_presets_and_folders() -> dict[str, Any]:
        sdir = Path(hub.cfg.state_dir)
        return {
            "ok": True,
            "campaign_presets": load_campaign_presets(sdir),
            "photo_presets": PHOTO_UNIQUIFIER_PRESETS,
            "sorted_folders": get_sorted_folders_summary(sdir),
        }

    @app.post("/api/uniquifier/preview")
    async def preview_uniquifier_api(
        file: UploadFile = File(...),
        photo_mode: str = Form("minimal_snow"),
    ) -> dict[str, Any]:
        raw = await file.read()
        if not raw:
            raise HTTPException(status_code=400, detail="Пустой файл изображения.")
        out_bytes = await asyncio.to_thread(uniquify_photo_bytes, raw, mode=photo_mode)
        return {
            "ok": True,
            "mode": photo_mode,
            "before_data_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii"),
            "after_data_url": "data:image/jpeg;base64," + base64.b64encode(out_bytes).decode("ascii"),
            "before_kb": round(len(raw) / 1024.0, 1),
            "after_kb": round(len(out_bytes) / 1024.0, 1),
        }

    @app.post("/api/accounts/{account_id}/restart")
    async def restart_tiktok_account(account_id: str, region: str = "") -> dict[str, Any]:
        acc = hub.store.load_account(account_id)
        jid = hub.start_job(f"Холодный рестарт TikTok @{account_id}", total=1)
        try:
            async with KintaroClient(hub.cfg) as client:
                pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                await pipeline.restart_tiktok_app(acc, target_region=region)
            hub.push_event(f"[{account_id}] 🔄 Приложение TikTok перезапущено (cold boot)", "success", kind="audit_updated")
            return {"ok": True, "account_id": account_id}
        finally:
            hub.step_job(jid)
            hub.end_job(jid)

    @app.post("/api/accounts/{account_id}/clean-posts")
    async def clean_old_posts_account(account_id: str) -> dict[str, Any]:
        acc = hub.store.load_account(account_id)
        jid = hub.start_job(f"Очистка старых постов @{account_id}", total=1)
        try:
            async with KintaroClient(hub.cfg) as client:
                pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                deleted = await pipeline.delete_all_account_posts(acc)
            if account_id in hub.audit_cache:
                hub.audit_cache[account_id]["post_penalties"] = []
                hub.audit_cache[account_id]["total_posts"] = 0
                hub.audit_cache[account_id]["total_views"] = 0
                hub._save_audit_cache()
            hub.push_event(f"[{account_id}] 🗑 Удалено старых постов: {deleted}", "success", kind="audit_updated")
            return {"ok": True, "account_id": account_id, "deleted": deleted}
        finally:
            hub.step_job(jid)
            hub.end_job(jid)

    @app.post("/api/campaign/run")
    async def run_campaign_api(
        files: list[UploadFile] = File(default=[]),
        server_media_files: str = Form(""),
        account_ids: str = Form(""),
        description: str = Form("#piratetoken #piratetokenshop #blackknight #pirateshop #fyp"),
        copies_count: int = Form(6),
        threads: int = Form(5),
        delay_min_sec: float = Form(14.0),
        delay_max_sec: float = Form(20.0),
        restart_every_n: int = Form(3),
        region: str = Form("US"),
        app_locale: str = Form("en_US"),
        photo_mode: str = Form("minimal_snow"),
        uniquifier_json: str = Form(""),
        auto_music_query: str = Form("Fortnite"),
        randomize_tags: bool = Form(True),
        delete_old_posts: bool = Form(True),
        auto_appeal_2048: bool = Form(True),
        appeal_quiet_window_sec: float = Form(60.0),
        pin_sticky_proxy: bool = Form(False),
        override_proxy_country: str = Form(""),
    ) -> dict[str, Any]:
        tmp_dir: Path | None = None
        saved_paths: list[str] = []
        if files:
            tmp_dir = Path(tempfile.mkdtemp(prefix="kintaro_camp_"))
            for uf in files:
                if not uf.filename:
                    continue
                suffix = Path(uf.filename).suffix.lower() or ".jpg"
                dest = tmp_dir / f"{uuid.uuid4().hex[:8]}{suffix}"
                dest.write_bytes(await uf.read())
                saved_paths.append(str(dest))
        if not saved_paths and server_media_files.strip():
            mdir = Path(hub.cfg.state_dir) / "media"
            for name in [x.strip() for x in server_media_files.split(",") if x.strip()]:
                fp = mdir / Path(name).name
                if fp.is_file():
                    saved_paths.append(str(fp))
        if not saved_paths:
            raise HTTPException(status_code=400, detail="Файлы не выбраны (загрузи или выбери из медиа-библиотеки).")

        uniq_overrides: dict[str, Any] = {}
        if uniquifier_json.strip():
            try:
                parsed_u = json.loads(uniquifier_json)
                if isinstance(parsed_u, dict):
                    uniq_overrides = parsed_u
            except Exception:
                pass

        is_video = Path(saved_paths[0]).suffix.lower() == ".mp4"
        targets = [x.strip() for x in account_ids.split(",") if x.strip()]
        camp = CampaignConfig(
            account_ids=targets,
            media_type="video" if is_video else "photo",
            media_paths=saved_paths,
            description=description,
            randomize_tags=randomize_tags,
            copies_count=copies_count,
            threads=threads,
            delay_min_sec=delay_min_sec,
            delay_max_sec=delay_max_sec,
            restart_every_n=restart_every_n,
            region=region,
            phone_region=region,
            app_locale=app_locale,
            pin_sticky_proxy=pin_sticky_proxy,
            override_proxy_country=override_proxy_country,
            delete_old_posts=delete_old_posts,
            photo_mode=photo_mode,
            uniquifier_overrides=uniq_overrides,
            auto_music_query=auto_music_query,
            auto_appeal_2048=auto_appeal_2048,
            appeal_quiet_window_sec=appeal_quiet_window_sec,
        )

        total_accs = len(targets) if targets else max(1, len(hub.store.list_accounts()))
        jid = hub.start_job(f"🚀 Кампания {copies_count}x ({photo_mode}, {threads} пот.)", total=total_accs)

        async def _bg_campaign() -> None:
            try:
                async with KintaroClient(hub.cfg) as client:
                    pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                    results = await pipeline.run_anti_shadow_campaign(
                        camp,
                        progress_cb=lambda aid, msg, meta: (
                            hub.push_event(f"[{aid}] {msg}", "info", kind="audit_updated"),
                            hub.step_job(jid) if meta.get("status") in ("completed", "partial", "error", "upload_failed") else None,
                        ),
                    )
                rep_file = Path(hub.cfg.state_dir) / "last_campaign_report.json"
                rep_file.write_text(
                    json.dumps([r.model_dump(mode="json") for r in results], indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                ok_n = sum(1 for r in results if r.status == "completed")
                tot_p = sum(r.posts_published for r in results)
                tot_a = sum(r.appeals_sent for r in results)
                hub.push_event(
                    f"🏁 Кампания завершена: {ok_n}/{len(results)} акк. | постов={tot_p} | апелляций 2048={tot_a}",
                    "success",
                    kind="audit_updated",
                )
            except Exception as exc:
                hub.push_event(f"❌ Ошибка кампании: {exc}", "error")
            finally:
                if tmp_dir is not None:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                hub.end_job(jid)

        asyncio.create_task(_bg_campaign())
        return {"ok": True, "started": True, "job_id": jid, "accounts": total_accs}

    @app.get("/api/campaign/last-report")
    async def get_last_campaign_report() -> JSONResponse:
        rep_file = Path(hub.cfg.state_dir) / "last_campaign_report.json"
        if not rep_file.is_file():
            return JSONResponse([])
        return JSONResponse(json.loads(rep_file.read_text(encoding="utf-8")))

    @app.post("/api/presets/save")
    async def save_preset_api(preset: dict[str, Any]) -> dict[str, Any]:
        items = save_campaign_preset(Path(hub.cfg.state_dir), preset)
        return {"ok": True, "campaign_presets": items}

    @app.delete("/api/presets/{preset_id}")
    async def delete_preset_api(preset_id: str) -> dict[str, Any]:
        items = delete_campaign_preset(Path(hub.cfg.state_dir), preset_id)
        return {"ok": True, "campaign_presets": items}

    # ── Server Media Library (`state/media/`) ────────────────────────────────
    @app.get("/api/media")
    async def list_media_api() -> dict[str, Any]:
        return {"ok": True, "files": list_media_library(Path(hub.cfg.state_dir))}

    @app.post("/api/media/upload")
    async def upload_media_library_api(files: list[UploadFile] = File(...)) -> dict[str, Any]:
        saved: list[str] = []
        sdir = Path(hub.cfg.state_dir)
        for uf in files:
            data = await uf.read()
            if data:
                saved.append(save_media_bytes(sdir, uf.filename or "creative.jpg", data))
        return {"ok": True, "saved": saved, "files": list_media_library(sdir)}

    @app.get("/api/media/file/{filename}")
    async def get_media_file_api(filename: str) -> FileResponse:
        fp = Path(hub.cfg.state_dir) / "media" / Path(filename).name
        if not fp.is_file():
            raise HTTPException(status_code=404, detail="Файл не найден")
        return FileResponse(fp)

    @app.delete("/api/media/{filename}")
    async def delete_media_api(filename: str) -> dict[str, Any]:
        ok = delete_media_file(Path(hub.cfg.state_dir), filename)
        return {"ok": ok, "files": list_media_library(Path(hub.cfg.state_dir))}

    # ── Sorted Folders & Bulk Session / CSV Exports ──────────────────────────
    @app.get("/api/folders")
    async def list_sorted_folders_api() -> dict[str, Any]:
        return {"ok": True, "folders": get_sorted_folders_summary(Path(hub.cfg.state_dir))}

    @app.get("/api/folders/{folder_key}/export")
    async def export_sorted_folder_api(folder_key: str, fmt: str = "txt") -> Response:
        spec = SORTED_FOLDER_SPECS.get(folder_key)
        if not spec:
            raise HTTPException(status_code=404, detail="Неизвестная папка")
        sdir = Path(hub.cfg.state_dir)
        if fmt == "zip":
            raw_zip = export_sorted_folder_zip(sdir, folder_key)
            return Response(
                content=raw_zip,
                media_type="application/zip",
                headers={"Content-Disposition": f'attachment; filename="{folder_key}_accounts.zip"'},
            )
        txt_path = sdir / "sorted_accounts" / folder_key / spec["filename"]
        txt_body = txt_path.read_text(encoding="utf-8") if txt_path.is_file() else ""
        return Response(
            content=txt_body.encode("utf-8"),
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{spec["filename"]}"'},
        )

    @app.get("/api/sessions/export/zip")
    async def export_sessions_zip_api() -> Response:
        return Response(
            content=export_all_sessions_zip(hub.store),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="kintaro_sessions.zip"'},
        )

    @app.get("/api/sessions/export/jsonl")
    async def export_sessions_jsonl_api() -> Response:
        return Response(
            content=export_all_sessions_jsonl(hub.store),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": 'attachment; filename="sessions.jsonl"'},
        )

    @app.post("/api/sessions/import-raw")
    async def import_raw_sessions_api(req: RawSessionsImportRequest) -> dict[str, Any]:
        px = normalize_proxy_url(req.default_proxy) if req.default_proxy.strip() else hub.default_proxy()
        if not px:
            raise HTTPException(status_code=400, detail="Укажи дефолтный прокси для импортируемых сессий.")
        imported = import_raw_sessions_text(hub.store, req.raw_text, default_proxy=px, default_region=req.default_region)
        hub.push_event(f"📥 Импортировано сырых x-device сессий: {len(imported)}", "success", kind="audit_updated")
        return {"ok": True, "imported": imported}

    @app.get("/api/analytics/export.csv")
    async def export_analytics_csv_api() -> Response:
        accs = [hub.serialize_account(a) for a in hub.store.list_accounts()]
        return Response(
            content=export_analytics_csv(accs),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="kintaro_analytics.csv"'},
        )

    @app.get("/api/system/info")
    async def system_info_api() -> dict[str, Any]:
        import platform as _plat
        return {
            "ok": True,
            "os": _plat.system(),
            "release": _plat.release(),
            "python": _plat.python_version(),
            "chromium_found": find_chromium_executable() or "playwright_bundled",
            "ffmpeg_found": bool(shutil.which(hub.cfg.uniquifier.ffmpeg_bin)),
        }

    # ── Spy Cloner, Top-Comment Pinning, Engagement Boost, Bulk Branding & Auto-Pilot ──
    @app.post("/api/spy")
    async def spy_api(req: SpyRequest) -> dict[str, Any]:
        acc = (
            hub.store.load_account(req.account_id)
            if req.account_id.strip()
            else next((a for a in hub.store.list_accounts() if a.authenticated), None)
        )
        if acc is None:
            raise HTTPException(status_code=400, detail="Нет авторизованного аккаунта для Spy-запроса.")
        async with KintaroClient(hub.cfg) as client:
            data = await spy_competitor_profile(client, acc, req.username, count=req.count)
        return {"ok": True, "spy": data}

    @app.post("/api/spy/download")
    async def spy_download_api(req: SpyDownloadRequest) -> dict[str, Any]:
        acc = (
            hub.store.load_account(req.account_id)
            if req.account_id.strip()
            else next((a for a in hub.store.list_accounts() if a.authenticated), None)
        )
        if acc is None:
            raise HTTPException(status_code=400, detail="Нет авторизованного аккаунта для скачивания крео.")
        async with KintaroClient(hub.cfg) as client:
            res = await download_creative_to_media(client, acc, req.target_post, Path(hub.cfg.state_dir))
        hub.push_event(f"🕵️ Склонировано без водянок в библиотеку: {', '.join(res['saved_files'])}", "success")
        return {"ok": True, **res}

    @app.post("/api/comments/list")
    async def comments_list_api(req: CommentsListRequest) -> dict[str, Any]:
        acc = (
            hub.store.load_account(req.account_id)
            if req.account_id.strip()
            else next((a for a in hub.store.list_accounts() if a.authenticated), None)
        )
        if acc is None:
            raise HTTPException(status_code=400, detail="Нет авторизованного аккаунта.")
        async with KintaroClient(hub.cfg) as client:
            data = await fetch_post_comments_structured(client, acc, req.target_post, count=req.count)
        return {"ok": True, **data}

    @app.post("/api/comments/boost")
    async def comments_boost_api(req: CommentsBoostRequest) -> dict[str, Any]:
        jid = hub.start_job("💬 Вывод коммента в ТОП #1", total=1)
        try:
            async with KintaroClient(hub.cfg) as client:
                res = await run_top_comment_boost(
                    client,
                    hub.store,
                    target_post=req.target_post,
                    seed_account_id=req.seed_account_id,
                    lead_comment_text=req.lead_comment_text,
                    existing_cid=req.existing_cid,
                    booster_account_ids=req.booster_account_ids,
                    reply_templates=req.reply_templates.splitlines(),
                    reply_prob_pct=req.reply_prob_pct,
                    threads=req.threads,
                )
            hub.push_event(
                f"💬 Коммент {res['cid']} забущен: лайков={res['likes_ok']}/{res['boosters_total']}, ответов={res['replies_ok']}",
                "success",
            )
            return {"ok": True, **res}
        finally:
            hub.step_job(jid)
            hub.end_job(jid)

    @app.post("/api/boost/run")
    async def engagement_boost_api(req: EngagementBoostRequest) -> dict[str, Any]:
        jid = hub.start_job("🔥 Взаимный буст поста/профиля", total=max(1, len(req.account_ids) or 1))
        try:
            async with KintaroClient(hub.cfg) as client:
                res = await run_engagement_boost(
                    client,
                    hub.store,
                    account_ids=req.account_ids or None,
                    target_post=req.target_post,
                    target_username=req.target_username,
                    do_view=req.do_view,
                    do_like=req.do_like,
                    do_collect=req.do_collect,
                    do_repost=req.do_repost,
                    comments_lines=req.comments_text.splitlines(),
                    threads=req.threads,
                    log_fn=lambda m: hub.push_event(m, "info"),
                )
            hub.push_event(f"🔥 Буст завершён: успешно {res['success']}/{res['total']}", "success")
            return {"ok": True, **res}
        finally:
            hub.end_job(jid)

    @app.post("/api/branding/run")
    async def bulk_branding_api(req: BrandingRequest) -> dict[str, Any]:
        av_bytes: bytes | None = None
        if req.avatar_data_url.strip():
            durl = req.avatar_data_url.strip()
            av_bytes = base64.b64decode(durl.split(",", 1)[1] if "," in durl else durl)
        jid = hub.start_job("🎨 Массовый оформитель профилей", total=max(1, len(req.account_ids) or 1))
        try:
            async with KintaroClient(hub.cfg) as client:
                res = await run_bulk_branding(
                    client,
                    hub.store,
                    account_ids=req.account_ids or None,
                    avatar_bytes=av_bytes,
                    uniquify_avatar=req.uniquify_avatar,
                    nickname_template=req.nickname_template,
                    username_prefix=req.username_prefix,
                    bio_template=req.bio_template,
                    bio_url=req.bio_url,
                    new_password=req.new_password,
                    enable_totp=req.enable_totp,
                    threads=req.threads,
                    log_fn=lambda m: hub.push_event(m, "info"),
                )
            hub.push_event(f"🎨 Оформление завершено: успешно {res['success']}/{res['total']}", "success", kind="audit_updated")
            return {"ok": True, **res}
        finally:
            hub.end_job(jid)

    async def _autopilot_runner(job: ScheduledJob) -> None:
        hub.push_event(f"⏰ Авто-Пилот запустил задачу `{job.action}` (#{job.id})...", "info")
        try:
            if job.action == "audit_all":
                await audit_all()
            elif job.action == "appeal_all":
                await appeal_all(AppealRequest())
            elif job.action == "shadow_check":
                await check_shadows_api()
        except Exception as exc:
            hub.push_event(f"⏰ Ошибка Авто-Пилота `{job.action}`: {exc}", "warn")

    autopilot = AutoPilotScheduler(_autopilot_runner)

    @app.get("/api/autopilot")
    async def list_autopilot_api() -> dict[str, Any]:
        return {"ok": True, "jobs": autopilot.list_jobs()}

    @app.post("/api/autopilot/add")
    async def add_autopilot_api(req: AutoPilotAddRequest) -> dict[str, Any]:
        if req.action not in ("audit_all", "appeal_all", "shadow_check"):
            raise HTTPException(status_code=400, detail="Поддерживаются действия: audit_all, appeal_all, shadow_check")
        j = autopilot.add_job(req.action, interval_minutes=req.interval_minutes, delay_minutes=req.delay_minutes, note=req.note)
        hub.push_event(f"⏰ Авто-Пилот: добавлена задача `{j.action}` каждые {j.interval_minutes} мин", "success")
        return {"ok": True, "job": j.to_dict(), "jobs": autopilot.list_jobs()}

    @app.delete("/api/autopilot/{job_id}")
    async def delete_autopilot_api(job_id: str) -> dict[str, Any]:
        autopilot.delete_job(job_id)
        return {"ok": True, "jobs": autopilot.list_jobs()}

    @app.post("/api/totp/generate")
    async def totp_generate_api(req: TotpRequest) -> dict[str, Any]:
        try:
            return {"ok": True, **generate_live_totp(req.secret)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return app

from __future__ import annotations

import asyncio
import json
import logging
import random
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from kintaro_auto.appeals import (
    WATCH_POLL_SECONDS,
    AccountAuditResult,
    audit_account_restrictions,
    remove_account_restrictions,
    set_post_visibility,
)
from kintaro_auto.client import KintaroClient
from kintaro_auto.config import AppConfig, load_config
from kintaro_auto.models import AccountRecord, UploadTask, apply_device_geo_profile
from kintaro_auto.pipeline import AutomationPipeline
from kintaro_auto.storage import StateStore, normalize_proxy_url, parse_shop_account_line

logger = logging.getLogger("kintaro_auto")


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


class DashboardHub:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path
        self.cfg: AppConfig = load_config(config_path)
        self.store = StateStore(self.cfg.state_dir)
        self.cache_file = Path(self.cfg.state_dir) / "audit_cache.json"
        self.audit_cache: dict[str, dict[str, Any]] = self._load_audit_cache()
        self.logs: list[dict[str, Any]] = []
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
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
            self.cache_file.write_text(json.dumps(self.audit_cache, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def reload_env(self) -> None:
        self.cfg = load_config(self.config_path)
        self.store = StateStore(self.cfg.state_dir)

    def push_event(self, text: str, level: str = "info", kind: str = "log", extra: dict[str, Any] | None = None) -> None:
        entry: dict[str, Any] = {
            "id": uuid.uuid4().hex[:10],
            "time": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "kind": kind,
            "text": text,
        }
        if extra:
            entry["extra"] = extra
        self.logs.append(entry)
        if len(self.logs) > 400:
            self.logs = self.logs[-400:]
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
            "proxy_masked": _mask_proxy(acc.proxy),
            "first_authenticated_at": acc.first_authenticated_at,
            "last_warmup_at": acc.last_warmup_at,
            "uploads_count": len(acc.upload_history),
            "audit": cached,
        }

    async def run_audit_one(self, account_id: str) -> dict[str, Any]:
        acc = self.store.load_account(account_id)
        self.push_event(f"[{account_id}] Проверка прокси + HTTP-аудит мобильного шлюза и Studio...", "info")
        aud: AccountAuditResult = await audit_account_restrictions(acc)
        self.store.save_account(acc)
        aud_dict = asdict(aud)
        aud_dict["audited_at"] = datetime.now().strftime("%H:%M:%S")
        self.audit_cache[account_id] = aud_dict
        self._save_audit_cache()
        self.push_event(
            f"[{account_id}] Аудит готов: @{aud.uniq_id} | {aud.reg_country or '?'}→{aud.current_region or '?'} | "
            f"Proxy={aud.proxy_country or 'OK'} | Health={aud.account_health.upper()} | Постов={aud.total_posts} (views={aud.total_views})",
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
                self.push_event(f"[Watchdog #{self.watchdog_cycles}] Проверка {len(targets)} аккаунтов...", "info")
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
                        sent = summary.notice_appeals_verified + summary.post_appeals_verified
                        if sent > 0 or summary.posts_hidden > 0:
                            self.push_event(
                                f"[{acc.account_id}] Снято флагов: {sent}, скрыто FAILED в Private: {summary.posts_hidden}",
                                "success",
                                kind="audit_updated",
                            )
                    except Exception as exc:
                        self.push_event(f"[{acc.account_id}] Ошибка сторожа: {exc}", "error")

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


class BatchImportRequest(BaseModel):
    lines_text: str
    proxy: str = ""
    region: str = "US"
    auto_login: bool = True
    auto_appeal: bool = True
    hide_rejected: bool = True


class AppealRequest(BaseModel):
    reason: str = "Our content is original, authentic, and strictly follows all TikTok Community Guidelines."
    dry_run: bool = False
    hide_rejected: bool = True


class PostVisibilityRequest(BaseModel):
    private: bool = True


class GeoSyncRequest(BaseModel):
    region: str = "US"


class WatchdogToggleRequest(BaseModel):
    enabled: bool
    hide_rejected: bool = True


def create_web_app(config_path: Path | None = None) -> FastAPI:
    hub = DashboardHub(config_path)
    app = FastAPI(title="Kintaro Auto · TikTok Control Center", version="2.0.0")

    class _HubLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            msg = record.getMessage()
            lvl = "error" if record.levelno >= logging.ERROR else ("warn" if record.levelno >= logging.WARNING else "info")
            hub.push_event(msg, lvl)

    lg = logging.getLogger("kintaro_auto")
    if not any(isinstance(h, _HubLogHandler) for h in lg.handlers):
        h = _HubLogHandler()
        h.setLevel(logging.INFO)
        lg.addHandler(h)
        lg.setLevel(logging.INFO)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return HTMLResponse(DASHBOARD_HTML)

    @app.get("/api/state")
    async def get_state() -> dict[str, Any]:
        hub.reload_env()
        accs = [hub.serialize_account(a) for a in hub.store.list_accounts()]
        return {
            "accounts": accs,
            "default_proxy_masked": _mask_proxy(hub.default_proxy()),
            "has_default_proxy": bool(hub.default_proxy()),
            "watchdog": {
                "enabled": hub.watchdog_enabled,
                "hide_rejected": hub.watchdog_hide_rejected,
                "cycles": hub.watchdog_cycles,
                "next_in_sec": max(0, int(hub.watchdog_next_run_ts - time.time()))
                if hub.watchdog_next_run_ts
                else None,
            },
            "logs": hub.logs[-80:],
        }

    @app.get("/api/events")
    async def sse_events() -> StreamingResponse:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        hub.subscribers.add(q)

        async def _gen():
            try:
                yield f"data: {json.dumps({'kind': 'connected', 'time': datetime.now().strftime('%H:%M:%S'), 'level': 'info', 'text': 'Подключено к live-потоку телеметрии'})}\n\n"
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
        sem = asyncio.Semaphore(3)

        async def _one(aid: str) -> None:
            async with sem:
                try:
                    await hub.run_audit_one(aid)
                except Exception as exc:
                    hub.push_event(f"[{aid}] Ошибка аудита: {exc}", "error")

        await asyncio.gather(*(_one(a.account_id) for a in targets))
        return {"ok": True, "audited": len(targets)}

    @app.post("/api/accounts/{account_id}/audit")
    async def audit_one(account_id: str) -> dict[str, Any]:
        try:
            res = await hub.run_audit_one(account_id)
            return {"ok": True, "audit": res}
        except Exception as exc:
            hub.push_event(f"[{account_id}] Ошибка аудита: {exc}", "error")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/accounts/{account_id}/appeal")
    async def appeal_one(account_id: str, req: AppealRequest) -> dict[str, Any]:
        try:
            acc = hub.store.load_account(account_id)
            hub.push_event(f"[{account_id}] Запуск 3-канального снятия огранов...", "info")
            summary = await remove_account_restrictions(
                acc,
                appeal_text=req.reason,
                dry_run=req.dry_run,
                hide_rejected=req.hide_rejected,
            )
            hub.store.save_account(acc)
            if summary.audit:
                ad = asdict(summary.audit)
                ad["audited_at"] = datetime.now().strftime("%H:%M:%S")
                hub.audit_cache[account_id] = ad
            hub.push_event(
                f"[{account_id}] Готово: подтверждено апелляций аккаунта={summary.notice_appeals_verified}, "
                f"постов={summary.post_appeals_verified}, скрыто в Private={summary.posts_hidden}",
                "success",
                kind="audit_updated",
            )
            return {"ok": True, "summary": asdict(summary)}
        except Exception as exc:
            hub.push_event(f"[{account_id}] Ошибка апелляции: {exc}", "error")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/appeal-all")
    async def appeal_all(req: AppealRequest) -> dict[str, Any]:
        targets = [a for a in hub.store.list_accounts() if a.authenticated and a.device]
        out = []
        for acc in targets:
            try:
                s = await remove_account_restrictions(
                    acc,
                    appeal_text=req.reason,
                    dry_run=req.dry_run,
                    hide_rejected=req.hide_rejected,
                )
                hub.store.save_account(acc)
                if s.audit:
                    ad = asdict(s.audit)
                    ad["audited_at"] = datetime.now().strftime("%H:%M:%S")
                    hub.audit_cache[acc.account_id] = ad
                out.append(asdict(s))
            except Exception as exc:
                hub.push_event(f"[{acc.account_id}] Ошибка апелляции: {exc}", "error")
        hub.push_event(f"Массовое снятие огранов завершено ({len(out)}/{len(targets)} акк.)", "success", kind="audit_updated")
        return {"ok": True, "results": out}

    @app.post("/api/accounts/{account_id}/auth")
    async def auth_account(account_id: str) -> dict[str, Any]:
        try:
            acc = hub.store.load_account(account_id)
            async with KintaroClient(hub.cfg) as client:
                pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                acc = await pipeline.ensure_authenticated(acc)
            hub.push_event(f"[{account_id}] Сессия активна! Запускаю первичный HTTP-аудит...", "success")
            await hub.run_audit_one(acc.account_id)
            return {"ok": True, "account": hub.serialize_account(acc)}
        except Exception as exc:
            hub.push_event(f"[{account_id}] Ошибка входа: {exc}", "error")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    @app.post("/api/accounts/{account_id}/warmup")
    async def warmup_one(account_id: str) -> dict[str, Any]:
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

    @app.post("/api/accounts/{account_id}/posts/{item_id}/visibility")
    async def change_visibility(account_id: str, item_id: str, req: PostVisibilityRequest) -> dict[str, Any]:
        acc = hub.store.load_account(account_id)
        ok, msg = await set_post_visibility(acc, item_id, private=req.private)
        hub.store.save_account(acc)
        hub.push_event(f"[{account_id}] {msg}", "success" if ok else "warn")
        if not ok:
            raise HTTPException(status_code=400, detail=msg)
        return {"ok": True, "message": msg}

    @app.delete("/api/accounts/{account_id}")
    async def delete_account(account_id: str) -> dict[str, Any]:
        p = hub.store.account_path(account_id)
        if p.exists():
            p.unlink()
        hub.audit_cache.pop(account_id, None)
        hub.push_event(f"Аккаунт [{account_id}] удалён из локального хранилища.", "warn", kind="audit_updated")
        return {"ok": True}

    @app.post("/api/import-batch")
    async def import_batch(req: BatchImportRequest) -> dict[str, Any]:
        fallback_px = normalize_proxy_url(req.proxy.strip()) if req.proxy.strip() else hub.default_proxy()
        lines = [ln.strip() for ln in req.lines_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        if not lines:
            raise HTTPException(status_code=400, detail="Список строк пуст.")

        imported_ids: list[str] = []
        for ln in lines:
            try:
                acc = parse_shop_account_line(ln, proxy=fallback_px, region=req.region)
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
            async with KintaroClient(hub.cfg) as client:
                pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                for aid in imported_ids:
                    acc = hub.store.load_account(aid)
                    if req.auto_login and (not acc.authenticated or not acc.device):
                        try:
                            hub.push_event(f"[{aid}] Авто-вход ({'Outlook Hub' if acc.use_outlook_hub else 'TOTP/Pass'})...", "info")
                            acc = await pipeline.ensure_authenticated(acc)
                            hub.push_event(f"[{aid}] Вход выполнен успешно!", "success", kind="audit_updated")
                        except Exception as exc:
                            hub.push_event(f"[{aid}] Ошибка входа: {exc}", "error")
                            continue
                    if req.auto_appeal and acc.authenticated and acc.device:
                        try:
                            s = await remove_account_restrictions(acc, hide_rejected=req.hide_rejected)
                            hub.store.save_account(acc)
                            if s.audit:
                                ad = asdict(s.audit)
                                ad["audited_at"] = datetime.now().strftime("%H:%M:%S")
                                hub.audit_cache[aid] = ad
                            hub.push_event(f"[{aid}] Проверка и снятие огранов завершены!", "success", kind="audit_updated")
                        except Exception as exc:
                            hub.push_event(f"[{aid}] Ошибка снятия огранов: {exc}", "error")

        if req.auto_login or req.auto_appeal:
            asyncio.create_task(_bg_pipeline())

        return {"ok": True, "imported": imported_ids}

    @app.post("/api/upload")
    async def upload_media(
        files: list[UploadFile] = File(...),
        account_id: str = Form(""),
        description: str = Form(""),
        music_search: str = Form(""),
        skip_warmup: bool = Form(False),
        force_cooldown: bool = Form(True),
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
        task = UploadTask(
            task_id=f"web_{uuid.uuid4().hex[:8]}",
            account_id=account_id.strip() or None,
            media_type="video" if is_video else "photo",
            video_path=str(saved_paths[0]) if is_video else None,
            photo_paths=[] if is_video else [str(p) for p in saved_paths],
            description=description,
            music_search_keyword=music_search.strip() or None,
        )

        try:
            async with KintaroClient(hub.cfg) as client:
                pipeline = AutomationPipeline(hub.cfg, hub.store, client)
                acc, rec = await pipeline.execute_upload_task(
                    task, force_cooldown=force_cooldown, skip_warmup=skip_warmup
                )
            hub.push_event(
                f"[{acc.account_id}] Пост успешно залит! aweme_id={rec.aweme_id}",
                "success",
                kind="audit_updated",
            )
            return {"ok": True, "account_id": acc.account_id, "aweme_id": rec.aweme_id}
        except Exception as exc:
            hub.push_event(f"Ошибка залива: {exc}", "error")
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    return app


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="ru" class="dark">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>KINTARO // TikTok Control & Unban Center</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {
    --bg: #06080d;
    --bg-elev: rgba(14, 18, 28, 0.82);
    --bg-card: rgba(17, 22, 34, 0.76);
    --bg-hover: rgba(26, 33, 50, 0.85);
    --border: rgba(255, 255, 255, 0.075);
    --border-strong: rgba(255, 255, 255, 0.14);
    --text: #f1f5f9;
    --muted: #8b9bb4;
    --accent: #6366f1;
    --accent-glow: rgba(99, 102, 241, 0.28);
    --emerald: #10b981;
    --emerald-bg: rgba(16, 185, 129, 0.12);
    --amber: #f59e0b;
    --amber-bg: rgba(245, 158, 11, 0.13);
    --rose: #f43f5e;
    --rose-bg: rgba(244, 63, 94, 0.13);
    --cyan: #06b6d4;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background:
      radial-gradient(900px 480px at 12% -8%, rgba(99, 102, 241, 0.16), transparent 65%),
      radial-gradient(800px 420px at 88% -5%, rgba(16, 185, 129, 0.11), transparent 60%),
      var(--bg);
    color: var(--text);
    min-height: 100vh;
    padding-bottom: 240px;
    -webkit-font-smoothing: antialiased;
  }
  .mono { font-family: 'JetBrains Mono', monospace; }

  /* Topbar */
  .topbar {
    position: sticky; top: 0; z-index: 40;
    backdrop-filter: blur(20px);
    background: rgba(6, 8, 13, 0.78);
    border-bottom: 1px solid var(--border);
    padding: 14px 28px;
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .brand-mark {
    width: 34px; height: 34px; border-radius: 10px;
    background: linear-gradient(135deg, #6366f1 0%, #06b6d4 100%);
    display: grid; place-items: center;
    font-weight: 700; font-size: 15px; color: #fff;
    box-shadow: 0 0 24px var(--accent-glow);
  }
  .brand-title { font-weight: 700; font-size: 15px; letter-spacing: -0.02em; }
  .brand-sub { font-size: 12px; color: var(--muted); }

  .watchdog-pill {
    display: flex; align-items: center; gap: 10px;
    padding: 7px 14px; border-radius: 999px;
    background: var(--bg-card); border: 1px solid var(--border);
    font-size: 12.5px;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #475569; }
  .dot.on { background: var(--emerald); box-shadow: 0 0 10px var(--emerald); animation: pulse 1.8s infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.45; } }

  /* Container */
  .wrap { max-width: 1380px; margin: 0 auto; padding: 24px 28px; }

  /* KPI Grid */
  .kpi-grid {
    display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 14px;
    margin-bottom: 22px;
  }
  @media (max-width: 1024px) { .kpi-grid { grid-template-columns: repeat(2, 1fr); } }
  .kpi {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 14px; padding: 16px 18px;
    transition: border-color .2s;
  }
  .kpi:hover { border-color: var(--border-strong); }
  .kpi-label { font-size: 12px; color: var(--muted); margin-bottom: 6px; display: flex; justify-content: space-between; }
  .kpi-val { font-size: 26px; font-weight: 700; letter-spacing: -0.03em; }

  /* Navigation Tabs & Toolbar */
  .nav-row {
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between;
    gap: 12px; margin-bottom: 20px;
  }
  .tabs {
    display: inline-flex; gap: 4px; padding: 4px;
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px;
  }
  .tab-btn {
    border: 0; background: transparent; color: var(--muted);
    padding: 8px 16px; border-radius: 9px; font-size: 13px; font-weight: 600;
    cursor: pointer; transition: all .16s;
  }
  .tab-btn.active {
    background: rgba(99, 102, 241, 0.18); color: #fff;
    box-shadow: inset 0 0 0 1px rgba(99, 102, 241, 0.45);
  }

  .btn {
    display: inline-flex; align-items: center; gap: 7px;
    border: 1px solid var(--border-strong); background: var(--bg-card);
    color: var(--text); padding: 8px 14px; border-radius: 10px;
    font-size: 12.5px; font-weight: 600; cursor: pointer; transition: all .15s;
  }
  .btn:hover:not(:disabled) { background: var(--bg-hover); transform: translateY(-1px); }
  .btn:disabled { opacity: 0.45; cursor: not-allowed; }
  .btn-primary {
    background: linear-gradient(135deg, #6366f1, #4f46e5);
    border-color: rgba(129, 140, 248, 0.4); color: #fff;
    box-shadow: 0 4px 18px rgba(99, 102, 241, 0.28);
  }
  .btn-emerald {
    background: rgba(16, 185, 129, 0.16); border-color: rgba(16, 185, 129, 0.4); color: #34d399;
  }
  .btn-rose {
    background: rgba(244, 63, 94, 0.14); border-color: rgba(244, 63, 94, 0.35); color: #fb7185;
  }
  .btn-sm { padding: 5px 10px; font-size: 11.5px; border-radius: 8px; }

  /* Filter Pills */
  .filters { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 16px; }
  .chip {
    padding: 5px 12px; border-radius: 999px; font-size: 12px; font-weight: 500;
    border: 1px solid var(--border); background: rgba(255,255,255,0.02);
    color: var(--muted); cursor: pointer;
  }
  .chip.active { border-color: var(--accent); color: #fff; background: rgba(99, 102, 241, 0.15); }

  /* Account Cards */
  .cards { display: grid; grid-template-columns: 1fr; gap: 14px; }
  .acc-card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 16px; padding: 18px 20px;
    transition: border-color .2s;
  }
  .acc-card:hover { border-color: var(--border-strong); }
  .acc-head {
    display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 14px;
  }
  .acc-id-group { display: flex; align-items: center; gap: 13px; }
  .avatar {
    width: 42px; height: 42px; border-radius: 12px;
    background: linear-gradient(135deg, rgba(99,102,241,0.28), rgba(6,182,212,0.22));
    border: 1px solid var(--border-strong);
    display: grid; place-items: center; font-weight: 700; font-size: 15px; overflow: hidden;
  }
  .avatar img { width: 100%; height: 100%; object-fit: cover; }
  .acc-handle { font-size: 15.5px; font-weight: 700; display: flex; align-items: center; gap: 8px; }
  .acc-meta { font-size: 12px; color: var(--muted); margin-top: 3px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }

  .badge {
    display: inline-flex; align-items: center; gap: 5px;
    padding: 3px 9px; border-radius: 999px; font-size: 11px; font-weight: 600;
  }
  .b-ok { background: var(--emerald-bg); color: #34d399; border: 1px solid rgba(16,185,129,0.3); }
  .b-warn { background: var(--amber-bg); color: #fbbf24; border: 1px solid rgba(245,158,11,0.3); }
  .b-err { background: var(--rose-bg); color: #fb7185; border: 1px solid rgba(244,63,94,0.3); }
  .b-neutral { background: rgba(148,163,184,0.12); color: #cbd5e1; border: 1px solid rgba(148,163,184,0.2); }

  /* Telemetry Strip inside Card */
  .tele-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 10px; margin-top: 15px; padding-top: 14px; border-top: 1px solid var(--border);
  }
  .tele-box {
    background: rgba(8, 11, 18, 0.55); border: 1px solid var(--border);
    border-radius: 11px; padding: 11px 13px; font-size: 12px;
  }
  .tele-title { color: var(--muted); font-size: 11px; margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.04em; }

  .alert-strip {
    margin-top: 12px; padding: 10px 13px; border-radius: 10px;
    font-size: 12px; display: flex; align-items: center; justify-content: space-between; gap: 10px;
  }
  .alert-warn { background: rgba(245, 158, 11, 0.09); border: 1px solid rgba(245, 158, 11, 0.28); color: #fcd34d; }
  .alert-ok { background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.24); color: #6ee7b7; }
  .alert-err { background: rgba(244, 63, 94, 0.1); border: 1px solid rgba(244, 63, 94, 0.3); color: #fda4af; }

  /* Studio Posts Table */
  .posts-wrap { margin-top: 14px; overflow-x: auto; }
  table.posts-tbl { width: 100%; border-collapse: collapse; font-size: 12px; }
  .posts-tbl th {
    text-align: left; color: var(--muted); font-weight: 500; padding: 8px 10px;
    border-bottom: 1px solid var(--border);
  }
  .posts-tbl td { padding: 8px 10px; border-bottom: 1px solid rgba(255,255,255,0.04); }
  .posts-tbl tr:hover td { background: rgba(255,255,255,0.02); }

  /* Forms */
  .panel {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 16px; padding: 22px;
  }
  .field { margin-bottom: 14px; }
  .field label { display: block; font-size: 12px; font-weight: 600; color: #cbd5e1; margin-bottom: 6px; }
  input[type="text"], select, textarea {
    width: 100%; background: rgba(6, 8, 14, 0.85); border: 1px solid var(--border-strong);
    color: var(--text); border-radius: 10px; padding: 10px 12px; font-size: 13px;
    outline: none; transition: border-color .15s;
  }
  input[type="text"]:focus, select:focus, textarea:focus { border-color: var(--accent); }
  textarea { min-height: 150px; resize: vertical; font-family: 'JetBrains Mono', monospace; font-size: 12px; }

  /* Live SSE Console Dock */
  .console-dock {
    position: fixed; bottom: 0; left: 0; right: 0; z-index: 50;
    background: rgba(7, 9, 15, 0.94); backdrop-filter: blur(18px);
    border-top: 1px solid var(--border-strong);
    height: 195px; display: flex; flex-direction: column;
    transition: height .2s;
  }
  .console-dock.collapsed { height: 38px; }
  .console-bar {
    padding: 8px 22px; border-bottom: 1px solid var(--border);
    display: flex; align-items: center; justify-content: space-between;
    font-size: 12px; font-weight: 600; cursor: pointer; user-select: none;
  }
  .console-body {
    flex: 1; overflow-y: auto; padding: 10px 22px;
    font-family: 'JetBrains Mono', monospace; font-size: 11.5px; line-height: 1.55;
  }
  .log-line { display: flex; gap: 10px; }
  .log-ts { color: #64748b; flex-shrink: 0; }
  .log-info { color: #cbd5e1; }
  .log-success { color: #34d399; }
  .log-warn { color: #fbbf24; }
  .log-error { color: #fb7185; }
  .hidden { display: none !important; }
</style>
</head>
<body>

<header class="topbar">
  <div class="brand">
    <div class="brand-mark">K</div>
    <div>
      <div class="brand-title">KINTARO // CONTROL & UNBAN CENTER</div>
      <div class="brand-sub">Mobile IDC Scanner · 3-Channel Appealer · Outlook Hub OAuth2 · Media Uniquifier</div>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:12px;">
    <div class="watchdog-pill">
      <span id="wdDot" class="dot"></span>
      <span>Сторож (Watchdog 95–165s): <strong id="wdStateTxt">OFF</strong></span>
      <button id="wdBtn" class="btn btn-sm" onclick="toggleWatchdog()">Включить</button>
    </div>
  </div>
</header>

<main class="wrap">
  <!-- KPI Strip -->
  <section class="kpi-grid">
    <div class="kpi">
      <div class="kpi-label"><span>Всего аккаунтов</span><span class="mono">POOL</span></div>
      <div class="kpi-val" id="kpiTotal">0</div>
    </div>
    <div class="kpi">
      <div class="kpi-label"><span>Рекомендации открыты</span><span style="color:var(--emerald)">ELIGIBLE</span></div>
      <div class="kpi-val" style="color:var(--emerald)" id="kpiEligible">0</div>
    </div>
    <div class="kpi">
      <div class="kpi-label"><span>На рассмотрении</span><span style="color:var(--amber)">APPEALING</span></div>
      <div class="kpi-val" style="color:var(--amber)" id="kpiAppealing">0</div>
    </div>
    <div class="kpi">
      <div class="kpi-label"><span>Требуют снятия ограна</span><span style="color:var(--rose)">ACTION</span></div>
      <div class="kpi-val" style="color:var(--rose)" id="kpiAction">0</div>
    </div>
    <div class="kpi">
      <div class="kpi-label"><span>Просмотров в Studio</span><span style="color:var(--cyan)">VIEWS</span></div>
      <div class="kpi-val mono" id="kpiViews">0</div>
    </div>
  </section>

  <!-- Tabs & Global Actions -->
  <div class="nav-row">
    <div class="tabs">
      <button class="tab-btn active" onclick="switchTab('monitor', this)">Мониторинг & Ограны</button>
      <button class="tab-btn" onclick="switchTab('import', this)">Импорт & Outlook Hub вход</button>
      <button class="tab-btn" onclick="switchTab('upload', this)">Анти-тень Залив</button>
    </div>
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
      <label style="font-size:12px;color:var(--muted);display:flex;align-items:center;gap:6px;cursor:pointer;">
        <input type="checkbox" id="chkHideRejected" checked /> Скрывать FAILED посты в Private
      </label>
      <button class="btn" id="btnAuditAll" onclick="auditAll()">↻ HTTP-аудит всех (0$)</button>
      <button class="btn btn-primary" id="btnAppealAll" onclick="appealAll()">⚡ Снять все ограны</button>
    </div>
  </div>

  <!-- TAB 1: MONITOR & UNBAN -->
  <section id="tab-monitor">
    <div class="filters">
      <button class="chip active" onclick="setFilter('all', this)">Все аккаунты</button>
      <button class="chip" onclick="setFilter('eligible', this)">Чистые / Restored</button>
      <button class="chip" onclick="setFilter('appealing', this)">На апелляции</button>
      <button class="chip" onclick="setFilter('action_required', this)">Активный огран</button>
      <button class="chip" onclick="setFilter('unauth', this)">Ожидают входа</button>
    </div>
    <div id="cardsList" class="cards"></div>
  </section>

  <!-- TAB 2: IMPORT & OUTLOOK HUB -->
  <section id="tab-import" class="hidden">
    <div class="panel">
      <h3 style="font-size:16px;margin-bottom:6px;">Пакетный импорт + Авто-вход (Outlook Hub OAuth2 / TikTok TOTP) + Снятие огранов</h3>
      <p style="font-size:12.5px;color:var(--muted);margin-bottom:16px;">
        Поддерживаются оба формата в одном списке: <code class="mono">username|password|email|totp[|proxy]</code> и
        <code class="mono">login:pass:mail@outlook.com:mail_pass:rec@polosmail.com:rec_pass:hotmail_2fa</code>
      </p>
      <div class="field">
        <label>Строки аккаунтов (по одной на строку)</label>
        <textarea id="impLines" placeholder="user|pass|mail@gmail.com|BASE32TOTP&#10;login:pass:mail@outlook.com:mail_pass:rec@polosmail.com:rec_pass:HOTMAIL2FA"></textarea>
      </div>
      <div style="display:grid;grid-template-columns:2fr 1fr;gap:14px;">
        <div class="field">
          <label>Прокси по умолчанию (оставь пустым, чтобы взять текущий сохранённый прокси пула: <span id="defProxyHint" class="mono"></span>)</label>
          <input type="text" id="impProxy" placeholder="socks5://user:pass@host:port или host:port:user:pass" />
        </div>
        <div class="field">
          <label>Целевой регион профиля</label>
          <select id="impRegion">
            <option value="US" selected>US — United States</option>
            <option value="DE">DE — Germany</option>
            <option value="GB">GB — United Kingdom</option>
          </select>
        </div>
      </div>
      <div style="display:flex;gap:18px;margin-bottom:16px;font-size:12.5px;">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
          <input type="checkbox" id="impAutoLogin" checked /> Авто-вход (Outlook Hub Graph API / TOTP)
        </label>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
          <input type="checkbox" id="impAutoAppeal" checked /> Сразу снять все ограны после входа
        </label>
      </div>
      <button class="btn btn-primary" onclick="submitBatchImport()">🚀 Запустить импорт и разблокировку</button>
    </div>
  </section>

  <!-- TAB 3: UPLOAD & UNIQUIFIER -->
  <section id="tab-upload" class="hidden">
    <div class="panel">
      <h3 style="font-size:16px;margin-bottom:6px;">Анти-тень Залив (FFmpeg / Pillow уникализатор + прогрев)</h3>
      <p style="font-size:12.5px;color:var(--muted);margin-bottom:16px;">
        Каждое видео/фото проходит зачистку метаданных, микро-кроп, сдвиг DCT/цвета под нативный Android MediaMuxer и публикуется через прокси аккаунта.
      </p>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;">
        <div class="field">
          <label>Аккаунт</label>
          <select id="upAccount"><option value="">Автовыбор самого отдохнувшего</option></select>
        </div>
        <div class="field">
          <label>Поиск музыки TikTok (опционально)</label>
          <input type="text" id="upMusic" placeholder="например: phonk,hoodtrap" />
        </div>
      </div>
      <div class="field">
        <label>Описание (поддерживает {Спинтакс|Варианты} и @mentions)</label>
        <input type="text" id="upDesc" placeholder="{Check this out|Wait for the end} #fyp" />
      </div>
      <div class="field">
        <label>Медиафайлы (1 MP4 видео ИЛИ от 1 до 10 JPG/PNG слайдов карусели)</label>
        <input type="file" id="upFiles" multiple accept=".mp4,.jpg,.jpeg,.png" style="padding:10px;background:rgba(6,8,14,0.85);border:1px dashed var(--border-strong);border-radius:10px;width:100%;" />
      </div>
      <div style="display:flex;gap:18px;margin-bottom:16px;font-size:12.5px;">
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
          <input type="checkbox" id="upSkipWarmup" /> Пропустить прогрев ленты перед заливом
        </label>
        <label style="display:flex;align-items:center;gap:6px;cursor:pointer;">
          <input type="checkbox" id="upForceCooldown" checked /> Игнорировать таймер отлёжки
        </label>
      </div>
      <button class="btn btn-primary" id="btnUpload" onclick="submitUpload()">📤 Уникализировать и опубликовать</button>
    </div>
  </section>
</main>

<!-- Live SSE Console -->
<footer id="consoleDock" class="console-dock">
  <div class="console-bar" onclick="document.getElementById('consoleDock').classList.toggle('collapsed')">
    <div style="display:flex;align-items:center;gap:8px;">
      <span class="dot on"></span>
      <span>LIVE ТЕЛЕМЕТРИЯ & ЛОГ ОПЕРАЦИЙ</span>
    </div>
    <span style="color:var(--muted);font-size:11px;">Нажми, чтобы свернуть / развернуть ▲▼</span>
  </div>
  <div id="consoleBody" class="console-body"></div>
</footer>

<script>
let STATE = { accounts: [], watchdog: { enabled: false } };
let ACTIVE_FILTER = 'all';
const OPEN_POSTS = new Set();

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function fmtTs(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
}

function appendLog(entry) {
  const box = document.getElementById('consoleBody');
  const div = document.createElement('div');
  div.className = 'log-line';
  const cls = 'log-' + (entry.level || 'info');
  div.innerHTML = `<span class="log-ts">[${esc(entry.time || '')}]</span><span class="${cls}">${esc(entry.text || '')}</span>`;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

async function fetchState() {
  const r = await fetch('/api/state');
  STATE = await r.json();
  renderAll();
}

function switchTab(name, btn) {
  ['monitor', 'import', 'upload'].forEach(t => {
    document.getElementById('tab-' + t).classList.toggle('hidden', t !== name);
  });
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

function setFilter(f, btn) {
  ACTIVE_FILTER = f;
  document.querySelectorAll('.chip').forEach(c => c.classList.remove('active'));
  btn.classList.add('active');
  renderCards();
}

function renderAll() {
  const accs = STATE.accounts || [];
  let elig = 0, app = 0, act = 0, views = 0;
  accs.forEach(a => {
    if (a.audit) {
      views += (a.audit.total_views || 0);
      if (a.audit.account_health === 'eligible') elig++;
      else if (a.audit.account_health === 'appealing') app++;
      else if (a.audit.account_health === 'action_required') act++;
    }
  });
  document.getElementById('kpiTotal').textContent = accs.length;
  document.getElementById('kpiEligible').textContent = elig;
  document.getElementById('kpiAppealing').textContent = app;
  document.getElementById('kpiAction').textContent = act;
  document.getElementById('kpiViews').textContent = views.toLocaleString('ru-RU');

  const wd = STATE.watchdog || {};
  document.getElementById('wdDot').className = 'dot' + (wd.enabled ? ' on' : '');
  document.getElementById('wdStateTxt').textContent = wd.enabled ? `ON (#${wd.cycles})` : 'OFF';
  document.getElementById('wdBtn').textContent = wd.enabled ? 'Остановить' : 'Включить';
  document.getElementById('defProxyHint').textContent = STATE.default_proxy_masked || 'не задан';

  const sel = document.getElementById('upAccount');
  const prev = sel.value;
  sel.innerHTML = '<option value="">Автовыбор самого отдохнувшего</option>' +
    accs.filter(a => a.authenticated).map(a => `<option value="${esc(a.account_id)}">@${esc(a.username)} (${esc(a.region)})</option>`).join('');
  sel.value = prev;

  renderCards();
}

function renderCards() {
  const box = document.getElementById('cardsList');
  const filtered = (STATE.accounts || []).filter(a => {
    if (ACTIVE_FILTER === 'all') return true;
    if (ACTIVE_FILTER === 'unauth') return !a.authenticated;
    return a.authenticated && a.audit && a.audit.account_health === ACTIVE_FILTER;
  });

  if (!filtered.length) {
    box.innerHTML = `<div class="panel" style="text-align:center;color:var(--muted);">Нет аккаунтов по выбранному фильтру. Нажми «↻ HTTP-аудит всех» или добавь строки во вкладке Импорта.</div>`;
    return;
  }

  box.innerHTML = filtered.map(a => {
    const aud = a.audit;
    let healthBadge = `<span class="badge b-neutral">Не проверен (нажми Аудит)</span>`;
    if (!a.authenticated) {
      healthBadge = `<span class="badge b-err">Требуется вход (Auth)</span>`;
    } else if (aud) {
      if (aud.account_health === 'eligible') healthBadge = `<span class="badge b-ok">● ELIGIBLE / РЕКОМЕНДАЦИИ ОТКРЫТЫ</span>`;
      else if (aud.account_health === 'appealing') healthBadge = `<span class="badge b-warn">● НА РАССМОТРЕНИИ (APPEALING)</span>`;
      else healthBadge = `<span class="badge b-err">● АКТИВНЫЙ ОГРАН (CAN APPEAL)</span>`;
    }

    const handle = aud?.uniq_id || a.username || a.account_id;
    const avatarHtml = aud?.avatar_url
      ? `<img src="${esc(aud.avatar_url)}" alt="" />`
      : esc(handle.slice(0, 2).toUpperCase());

    const regPair = aud ? `${esc(aud.reg_country || '?')} → ${esc(aud.current_region || '?')}` : `Target: ${esc(a.region)}`;
    const needGeoSync = aud && aud.current_region && aud.current_region !== a.region;
    const idcShort = aud?.idc_gateway ? aud.idc_gateway.replace('https://api16-normal-', '') : '—';
    const proxyBadge = aud?.proxy_country
      ? `<span class="badge b-ok mono">${esc(aud.proxy_country)} · ${esc(aud.proxy_ip)}</span>`
      : `<span class="mono" style="color:var(--muted)">${esc(a.proxy_masked)}</span>`;

    let vioBlocks = '';
    if (aud) {
      if (aud.region_warning_present) {
        vioBlocks += `<div class="alert-strip alert-warn">
          <span>⚠️ В системных уведомлениях есть архивная плашка <strong>Update your account region</strong> (ранее фиксировался вход не с родного GEO). Сейчас все запросы жестко заблокированы под прокси аккаунта.</span>
        </div>`;
      }
      (aud.notice_violations || []).forEach(v => {
        const cls = v.can_appeal ? 'alert-err' : 'alert-warn';
        vioBlocks += `<div class="alert-strip ${cls}">
          <div>
            <strong>${esc(v.title)}</strong> <span class="mono" style="opacity:.8">punish_id=${esc(v.punish_id)}</span><br/>
            <span style="opacity:.9">Статус: <strong>${esc(v.status_label)}</strong> | Source: <code class="mono">${esc(v.punish_source || '—')}</code> | До: ${fmtTs(v.punish_end_time)} ${v.appeal_id && v.appeal_id !== '0' ? '| AppealId: ' + esc(v.appeal_id) : ''}</span>
          </div>
          ${v.can_appeal ? `<button class="btn btn-sm btn-primary" onclick="appealAccount('${esc(a.account_id)}')">Подать апелляцию</button>` : ''}
        </div>`;
      });
      if ((!aud.notice_violations || !aud.notice_violations.length) && (aud.restored_notices || []).length) {
        const r0 = aud.restored_notices[0];
        vioBlocks += `<div class="alert-strip alert-ok">
          <span>✓ Огран аккаунта успешно снят (<strong>${esc(r0.title)}</strong> · ${fmtTs(r0.create_time)} · punish_id=${esc(r0.punish_id)})</span>
        </div>`;
      }
    }

    const showPosts = OPEN_POSTS.has(a.account_id);
    const flaggedCount = aud ? (aud.post_penalties || []).filter(p => p.appeal_status !== 2 || p.v2_can_appeal).length : 0;

    let postsHtml = '';
    if (aud && showPosts) {
      const rows = (aud.post_penalties || []).map(p => {
        const isOk = p.appeal_status === 2 && !p.v2_can_appeal;
        const stBadge = isOk
          ? `<span class="badge b-ok">OK</span>`
          : `<span class="badge ${p.appeal_status === 0 || p.v2_can_appeal ? 'b-err' : 'b-warn'}">${esc(p.appeal_status_label)} ${esc((p.reason_labels||[]).join(','))}</span>`;
        return `<tr>
          <td class="mono"><a href="https://www.tiktok.com/@${esc(handle)}/video/${esc(p.item_id)}" target="_blank" style="color:#93c5fd;text-decoration:none;">${esc(p.item_id)} ↗</a></td>
          <td>${fmtTs(p.create_time)}</td>
          <td class="mono" style="font-weight:600">${(p.play_count||0).toLocaleString()}</td>
          <td>${esc((p.desc || '<без описания>').slice(0, 56))}</td>
          <td>${stBadge}</td>
          <td style="text-align:right">
            <button class="btn btn-sm" onclick="setPostPrivate('${esc(a.account_id)}','${esc(p.item_id)}',true)">В Private</button>
          </td>
        </tr>`;
      }).join('');
      postsHtml = `<div class="posts-wrap">
        <table class="posts-tbl">
          <thead><tr><th>Aweme ID</th><th>Дата</th><th>Просмотры</th><th>Описание</th><th>Модерация FYP</th><th></th></tr></thead>
          <tbody>${rows || '<tr><td colspan="6" style="color:var(--muted)">Постов пока нет</td></tr>'}</tbody>
        </table>
      </div>`;
    }

    return `<div class="acc-card">
      <div class="acc-head">
        <div class="acc-id-group">
          <div class="avatar">${avatarHtml}</div>
          <div>
            <div class="acc-handle">
              <span>@${esc(handle)}</span>
              ${healthBadge}
              ${a.use_outlook_hub ? '<span class="badge b-neutral">Outlook Hub</span>' : ''}
              ${a.has_totp ? '<span class="badge b-neutral">2FA TOTP</span>' : ''}
            </div>
            <div class="acc-meta mono">
              <span>ID: ${esc(aud?.uid || '—')}</span> ·
              <span>Кластер: ${esc(idcShort)}</span> ·
              <span>Гео: ${regPair}</span>
              ${needGeoSync ? `<button class="btn btn-sm" onclick="syncGeo('${esc(a.account_id)}','${esc(a.region)}')">Sync ${esc(a.region)} SIM</button>` : ''}
            </div>
          </div>
        </div>
        <div style="display:flex;gap:7px;flex-wrap:wrap;">
          ${!a.authenticated ? `<button class="btn btn-sm btn-primary" onclick="authAccount('${esc(a.account_id)}')">🔑 Войти</button>` : ''}
          ${a.authenticated ? `<button class="btn btn-sm" onclick="auditAccount('${esc(a.account_id)}')">↻ Аудит</button>` : ''}
          ${a.authenticated ? `<button class="btn btn-sm btn-emerald" onclick="appealAccount('${esc(a.account_id)}')">⚡ Снять ограны</button>` : ''}
          ${a.authenticated ? `<button class="btn btn-sm" onclick="warmupAccount('${esc(a.account_id)}')">🔥 Прогрев</button>` : ''}
          ${aud ? `<button class="btn btn-sm" onclick="togglePosts('${esc(a.account_id)}')">📹 Посты (${aud.total_posts}${flaggedCount ? ' · ⚠ ' + flaggedCount : ''})</button>` : ''}
          <button class="btn btn-sm btn-rose" onclick="deleteAccount('${esc(a.account_id)}')">✕</button>
        </div>
      </div>

      ${aud ? `<div class="tele-grid">
        <div class="tele-box">
          <div class="tele-title">Защита прокси (Exit Node)</div>
          <div>${proxyBadge}</div>
        </div>
        <div class="tele-box">
          <div class="tele-title">Статистика TikTok Studio</div>
          <div>Постов: <strong>${aud.total_posts}</strong> · Просмотров: <strong class="mono">${aud.total_views.toLocaleString()}</strong> · Флагов: <strong style="color:${flaggedCount ? 'var(--rose)' : 'var(--emerald)'}">${flaggedCount}</strong></div>
        </div>
        <div class="tele-box">
          <div class="tele-title">Последний вход в уведах</div>
          <div style="white-space:nowrap;overflow:hidden;text-overflow:ellipsis;" title="${esc((aud.recent_device_logins||[])[0] || 'Нет записей')}">${esc((aud.recent_device_logins||[])[0] || 'Чисто — новых устройств нет')}</div>
        </div>
      </div>` : ''}

      ${vioBlocks}
      ${postsHtml}
    </div>`;
  }).join('');
}

function togglePosts(aid) {
  if (OPEN_POSTS.has(aid)) OPEN_POSTS.delete(aid); else OPEN_POSTS.add(aid);
  renderCards();
}

async function apiPost(url, body = {}) {
  const r = await fetch(url, { method: 'POST', headers: {'content-type':'application/json'}, body: JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('HTTP ' + r.status));
  return data;
}

async function auditAll() {
  const btn = document.getElementById('btnAuditAll');
  btn.disabled = true;
  try { await apiPost('/api/audit-all'); await fetchState(); }
  catch (e) { alert(e.message); }
  finally { btn.disabled = false; }
}

async function appealAll() {
  const btn = document.getElementById('btnAppealAll');
  btn.disabled = true;
  try {
    await apiPost('/api/appeal-all', { hide_rejected: document.getElementById('chkHideRejected').checked });
    await fetchState();
  } catch (e) { alert(e.message); }
  finally { btn.disabled = false; }
}

async function auditAccount(aid) {
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/audit`); await fetchState(); }
  catch (e) { alert(e.message); }
}

async function appealAccount(aid) {
  try {
    await apiPost(`/api/accounts/${encodeURIComponent(aid)}/appeal`, { hide_rejected: document.getElementById('chkHideRejected').checked });
    await fetchState();
  } catch (e) { alert(e.message); }
}

async function authAccount(aid) {
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/auth`); await fetchState(); }
  catch (e) { alert(e.message); }
}

async function syncGeo(aid, reg) {
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/sync-geo`, { region: reg }); await fetchState(); }
  catch (e) { alert(e.message); }
}

async function warmupAccount(aid) {
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/warmup`); await fetchState(); }
  catch (e) { alert(e.message); }
}

async function setPostPrivate(aid, itemId, priv) {
  try { await apiPost(`/api/accounts/${encodeURIComponent(aid)}/posts/${encodeURIComponent(itemId)}/visibility`, { private: priv }); }
  catch (e) { alert(e.message); }
}

async function deleteAccount(aid) {
  if (!confirm(`Удалить аккаунт ${aid} из локальной панели?`)) return;
  await fetch(`/api/accounts/${encodeURIComponent(aid)}`, { method: 'DELETE' });
  await fetchState();
}

async function toggleWatchdog() {
  const next = !(STATE.watchdog && STATE.watchdog.enabled);
  await apiPost('/api/watchdog', { enabled: next, hide_rejected: document.getElementById('chkHideRejected').checked });
  await fetchState();
}

async function submitBatchImport() {
  const lines_text = document.getElementById('impLines').value;
  const proxy = document.getElementById('impProxy').value;
  const region = document.getElementById('impRegion').value;
  const auto_login = document.getElementById('impAutoLogin').checked;
  const auto_appeal = document.getElementById('impAutoAppeal').checked;
  const hide_rejected = document.getElementById('chkHideRejected').checked;
  try {
    await apiPost('/api/import-batch', { lines_text, proxy, region, auto_login, auto_appeal, hide_rejected });
    document.getElementById('impLines').value = '';
    switchTab('monitor', document.querySelector('.tab-btn'));
    await fetchState();
  } catch (e) { alert(e.message); }
}

async function submitUpload() {
  const files = document.getElementById('upFiles').files;
  if (!files.length) return alert('Выбери MP4 или фото!');
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('account_id', document.getElementById('upAccount').value);
  fd.append('description', document.getElementById('upDesc').value);
  fd.append('music_search', document.getElementById('upMusic').value);
  fd.append('skip_warmup', document.getElementById('upSkipWarmup').checked);
  fd.append('force_cooldown', document.getElementById('upForceCooldown').checked);

  const btn = document.getElementById('btnUpload');
  btn.disabled = true;
  try {
    const r = await fetch('/api/upload', { method: 'POST', body: fd });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || 'Upload error');
    alert(`Пост опубликован на @${d.account_id}! aweme_id=${d.aweme_id}`);
    await fetchState();
  } catch (e) { alert(e.message); }
  finally { btn.disabled = false; }
}

(async function boot() {
  await fetchState();
  (STATE.logs || []).forEach(appendLog);
  const es = new EventSource('/api/events');
  es.onmessage = (ev) => {
    try {
      const item = JSON.parse(ev.data);
      appendLog(item);
      if (item.kind === 'audit_updated' || item.kind === 'watchdog') fetchState();
    } catch (_) {}
  };
})();
</script>
</body>
</html>
"""

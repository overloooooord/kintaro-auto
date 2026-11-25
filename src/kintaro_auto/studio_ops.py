"""
Studio Farm Operations: Target Engagement Boost, Bulk Profile Branding, ZIP/CSV/JSONL Exports,
Raw x-device Bulk Importer, and Auto-Pilot Scheduler (merged cleanly from swegby/kintaro_auth).
"""

from __future__ import annotations

import asyncio
import base64
import csv
import io
import json
import random
import re
import time
import uuid
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kintaro_auto.appeals import SORTED_FOLDER_SPECS
from kintaro_auto.captions import resolve_spintax
from kintaro_auto.client import KintaroClient
from kintaro_auto.models import AccountRecord
from kintaro_auto.spy import extract_aweme_id_from_input
from kintaro_auto.storage import StateStore
from kintaro_auto.uniquifier import uniquify_photo_bytes


def _extract_totp_from_response(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("totp_secret", "secret", "totp_token", "totp_key", "key"):
        val = body.get(key)
        if isinstance(val, str) and len(val.strip()) >= 16:
            return val.strip()
    data = body.get("data")
    if isinstance(data, dict):
        return _extract_totp_from_response(data)
    return ""


async def run_engagement_boost(
    client: KintaroClient,
    store: StateStore,
    *,
    account_ids: list[str] | None = None,
    target_post: str = "",
    target_username: str = "",
    do_view: bool = True,
    do_like: bool = True,
    do_collect: bool = False,
    do_repost: bool = False,
    comments_lines: list[str] | None = None,
    threads: int = 4,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Multi-account mutual engagement boost (views, likes, favorites, reposts, comments, follows)."""
    aid = extract_aweme_id_from_input(target_post)
    uname = target_username.lstrip("@").strip()
    if not aid and not uname:
        raise ValueError("Укажи целевой пост (ссылка/aweme_id) или @username для подписки.")

    targets = (
        [store.load_account(x) for x in account_ids]
        if account_ids
        else [a for a in store.list_accounts() if a.authenticated]
    )
    if not targets:
        raise RuntimeError("Нет авторизованных аккаунтов для буста.")

    sem = asyncio.Semaphore(max(1, min(15, threads)))
    clean_cmts = [c.strip() for c in (comments_lines or []) if c.strip()]
    ok_cnt = 0
    results: list[dict[str, Any]] = []

    async def _one(acc: AccountRecord) -> None:
        nonlocal ok_cnt
        async with sem:
            actions: list[str] = []
            try:
                if uname:
                    await client.follow_user(acc, uname)
                    actions.append(f"follow @{uname}")
                if aid:
                    if do_view:
                        await client.view_post(acc, aid, random.randint(6000, 14500))
                        actions.append("view")
                    if do_like:
                        await client.like_post(acc, aid)
                        actions.append("like")
                    if do_collect:
                        await client.collect_post(acc, aid)
                        actions.append("collect")
                    if do_repost:
                        await client.repost_post(acc, aid)
                        actions.append("repost")
                    if clean_cmts:
                        txt = resolve_spintax(random.choice(clean_cmts))
                        await client.publish_comment(acc, aid, txt)
                        actions.append("comment")
                store.save_account(acc)
                ok_cnt += 1
                msg = ", ".join(actions) or "ok"
                if log_fn:
                    log_fn(f"[{acc.account_id}] 🚀 Буст выполнен: {msg}")
                results.append({"account_id": acc.account_id, "ok": True, "detail": msg})
            except Exception as exc:
                if log_fn:
                    log_fn(f"[{acc.account_id}] ⚠️ Ошибка буста: {exc}")
                results.append({"account_id": acc.account_id, "ok": False, "error": str(exc)})

    await asyncio.gather(*(_one(a) for a in targets))
    return {"total": len(targets), "success": ok_cnt, "results": results}


async def run_bulk_branding(
    client: KintaroClient,
    store: StateStore,
    *,
    account_ids: list[str] | None = None,
    avatar_bytes: bytes | None = None,
    uniquify_avatar: bool = True,
    nickname_template: str = "",
    username_prefix: str = "",
    bio_template: str = "",
    bio_url: str = "",
    new_password: str = "",
    enable_totp: bool = False,
    threads: int = 3,
    log_fn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Bulk profile branding studio: uniquified avatar, spintax nickname/bio, bio link, username, password, TOTP."""
    targets = (
        [store.load_account(x) for x in account_ids]
        if account_ids
        else [a for a in store.list_accounts() if a.authenticated]
    )
    if not targets:
        raise RuntimeError("Нет авторизованных аккаунтов для оформления.")

    clean_prefix = re.sub(r"[^a-z0-9_.]", "", username_prefix.strip().lower())[:14]
    sem = asyncio.Semaphore(max(1, min(15, threads)))
    ok_cnt = 0
    results: list[dict[str, Any]] = []

    async def _one(acc: AccountRecord) -> None:
        nonlocal ok_cnt
        async with sem:
            done: list[str] = []
            try:
                if avatar_bytes:
                    av = uniquify_photo_bytes(avatar_bytes, mode="stealth") if uniquify_avatar else avatar_bytes
                    await client.set_avatar(acc, base64.b64encode(av).decode("ascii"))
                    done.append("avatar")
                if nickname_template.strip():
                    nick = resolve_spintax(nickname_template.strip())
                    await client.set_nickname(acc, nick)
                    done.append(f"nick={nick}")
                if clean_prefix:
                    new_u = f"{clean_prefix}{random.randint(1000, 99999)}"
                    await client.change_username(acc, new_u)
                    acc.username = new_u
                    done.append(f"@{new_u}")
                if bio_template.strip():
                    bio = resolve_spintax(bio_template.strip())
                    await client.set_bio(acc, bio)
                    done.append("bio")
                if bio_url.strip():
                    await client.set_bio_url(acc, bio_url.strip())
                    done.append("bio_url")
                if new_password.strip():
                    await client.set_password(acc, new_password.strip())
                    acc.password = new_password.strip()
                    done.append("password")
                if enable_totp:
                    for way in ("email", "mobile_sms", "pwd"):
                        try:
                            tb = await client.enable_totp(acc, additional_way=way)
                            sec = _extract_totp_from_response(tb)
                            if sec:
                                acc.totp_token = sec
                                done.append(f"TOTP={sec}")
                                break
                        except Exception:
                            continue
                store.save_account(acc)
                ok_cnt += 1
                msg = ", ".join(done) or "без изменений"
                if log_fn:
                    log_fn(f"[{acc.account_id}] 🎨 Профиль обновлён: {msg}")
                results.append({"account_id": acc.account_id, "ok": True, "detail": msg})
            except Exception as exc:
                if log_fn:
                    log_fn(f"[{acc.account_id}] ❌ Ошибка оформления: {exc}")
                results.append({"account_id": acc.account_id, "ok": False, "error": str(exc)})

    await asyncio.gather(*(_one(a) for a in targets))
    return {"total": len(targets), "success": ok_cnt, "results": results}


# ── Exports (ZIP, JSONL, Excel CSV) & Raw Session Importer ───────────────────


def export_sorted_folder_zip(state_dir: Path, folder_key: str) -> bytes:
    spec = SORTED_FOLDER_SPECS.get(folder_key)
    if not spec:
        raise ValueError(f"Неизвестная папка: {folder_key}")
    fdir = Path(state_dir) / "sorted_accounts" / folder_key
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        txt = fdir / spec["filename"]
        if txt.is_file():
            zf.write(txt, arcname=spec["filename"])
        sdir = fdir / "sessions"
        if sdir.is_dir():
            for fp in sorted(sdir.glob("*.json")):
                zf.write(fp, arcname=f"sessions/{fp.name}")
    return buf.getvalue()


def export_all_sessions_jsonl(store: StateStore) -> bytes:
    lines = [acc.model_dump_json() for acc in store.list_accounts()]
    return ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")


def export_all_sessions_zip(store: StateStore) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for acc in store.list_accounts():
            zf.writestr(f"sessions/{acc.account_id}.json", acc.model_dump_json(indent=2))
        zf.writestr("sessions.jsonl", export_all_sessions_jsonl(store))
    return buf.getvalue()


def export_analytics_csv(accounts_payload: list[dict[str, Any]]) -> bytes:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "account_id",
            "username",
            "region",
            "authenticated",
            "health",
            "reg_country",
            "current_region",
            "idc_gateway",
            "total_posts",
            "total_views",
            "active_notices",
            "proxy_country",
        ]
    )
    for a in accounts_payload:
        aud = a.get("audit") or {}
        writer.writerow(
            [
                a.get("account_id", ""),
                aud.get("uniq_id") or a.get("username", ""),
                a.get("region", ""),
                "yes" if a.get("authenticated") else "no",
                aud.get("account_health", "unknown"),
                aud.get("reg_country", ""),
                aud.get("current_region", ""),
                aud.get("idc_gateway", ""),
                aud.get("total_posts", a.get("uploads_count", 0)),
                aud.get("total_views", 0),
                len(aud.get("notice_violations") or []),
                aud.get("proxy_country", ""),
            ]
        )
    # UTF-8 BOM so Microsoft Excel on Windows opens Russian text cleanly without mojibake!
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def import_raw_sessions_text(
    store: StateStore,
    raw_text: str,
    *,
    default_proxy: str,
    default_region: str = "US",
) -> list[str]:
    """Imports pasted JSON, JSON array, or JSONL containing `x-device` / `AccountRecord` objects."""
    text = (raw_text or "").strip()
    if not text:
        return []
    objs: list[dict[str, Any]] = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            objs = [x for x in parsed if isinstance(x, dict)]
        elif isinstance(parsed, dict):
            objs = [parsed]
    except json.JSONDecodeError:
        for line in text.splitlines():
            ln = line.strip()
            if not ln:
                continue
            try:
                obj = json.loads(ln)
                if isinstance(obj, dict):
                    objs.append(obj)
            except json.JSONDecodeError:
                continue

    imported: list[str] = []
    for idx, obj in enumerate(objs, start=1):
        dev = obj.get("device") or obj.get("x_device")
        if isinstance(dev, str) and dev.strip().startswith("{"):
            dev = json.loads(dev)
        if not isinstance(dev, dict) and ("cookies" in obj or "device_id" in obj):
            dev = obj
        if not isinstance(dev, dict):
            continue
        uname = str(obj.get("username") or obj.get("account_id") or f"sess_{int(time.time())}_{idx}").strip().lstrip("@")
        px = str(obj.get("proxy") or default_proxy).strip()
        reg = str(obj.get("region") or dev.get("region") or default_region).strip().upper()[:2] or "US"
        acc = AccountRecord(
            account_id=uname,
            username=uname,
            email=str(obj.get("email") or ""),
            proxy=px,
            region=reg,
            auth_method="device_only",
            device=dev,
            authenticated=True,
        )
        store.save_account(acc)
        imported.append(uname)
    return imported


# ── Auto-Pilot Scheduler ─────────────────────────────────────────────────────


@dataclass
class ScheduledJob:
    id: str
    action: str  # "audit_all" | "appeal_all" | "shadow_check"
    interval_minutes: int
    next_run_ts: float
    last_run_at: str = ""
    runs_count: int = 0
    enabled: bool = True
    note: str = ""
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "action": self.action,
            "interval_minutes": self.interval_minutes,
            "next_run_in_sec": max(0, int(self.next_run_ts - time.time())),
            "last_run_at": self.last_run_at,
            "runs_count": self.runs_count,
            "enabled": self.enabled,
            "note": self.note,
        }


class AutoPilotScheduler:
    """Lightweight async scheduler for periodic audit, 2048 appeal sweeps, and shadowban checks."""

    def __init__(self, runner: Callable[[ScheduledJob], Awaitable[None]]) -> None:
        self._runner = runner
        self.jobs: dict[str, ScheduledJob] = {}
        self._task: asyncio.Task[None] | None = None

    def ensure_started(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop())

    def add_job(
        self,
        action: str,
        *,
        interval_minutes: int = 30,
        delay_minutes: int = 0,
        note: str = "",
        params: dict[str, Any] | None = None,
    ) -> ScheduledJob:
        jid = uuid.uuid4().hex[:8]
        first_wait = max(0, delay_minutes) * 60 if delay_minutes > 0 else max(1, interval_minutes) * 60
        job = ScheduledJob(
            id=jid,
            action=action,
            interval_minutes=max(1, interval_minutes),
            next_run_ts=time.time() + first_wait,
            note=note,
            params=dict(params or {}),
        )
        self.jobs[jid] = job
        self.ensure_started()
        return job

    def delete_job(self, job_id: str) -> bool:
        return self.jobs.pop(job_id, None) is not None

    def list_jobs(self) -> list[dict[str, Any]]:
        return [j.to_dict() for j in self.jobs.values()]

    async def _loop(self) -> None:
        while True:
            try:
                now = time.time()
                for job in list(self.jobs.values()):
                    if job.enabled and now >= job.next_run_ts:
                        job.next_run_ts = now + job.interval_minutes * 60
                        job.runs_count += 1
                        job.last_run_at = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                        asyncio.create_task(self._runner(job))
            except Exception:
                pass
            await asyncio.sleep(5.0)

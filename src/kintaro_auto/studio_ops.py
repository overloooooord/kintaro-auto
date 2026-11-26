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
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kintaro_auto.appeals import (
    SORTED_FOLDER_SPECS,
    classify_appeal_folder,
    remove_account_restrictions,
    sort_account_into_folder,
    wait_and_appeal_settle,
)
from kintaro_auto.captions import resolve_spintax
from kintaro_auto.client import KintaroClient
from kintaro_auto.models import AccountRecord
from kintaro_auto.spy import extract_aweme_id_from_input
from kintaro_auto.storage import StateStore, format_account_copy_line
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


# ── 1-Click Copy Export for Uploaded / Filtered / Sorted Accounts ────────────


def _load_last_campaign_report_map(state_dir: Path) -> tuple[list[str], dict[str, dict[str, Any]]]:
    rep_file = Path(state_dir) / "last_campaign_report.json"
    if not rep_file.is_file():
        return [], {}
    try:
        raw = json.loads(rep_file.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return [], {}
    except Exception:
        return [], {}
    order: list[str] = []
    by_id: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("account_id") or "").strip()
        if aid:
            order.append(aid)
            by_id[aid] = item
    return order, by_id


def _load_sorted_folder_entries(state_dir: Path, folder_key: str) -> list[tuple[str, str]]:
    """Returns list of (account_id_or_handle, raw_line) from `state/sorted_accounts/<folder_key>/`."""
    spec = SORTED_FOLDER_SPECS.get(folder_key)
    if not spec:
        return []
    fdir = Path(state_dir) / "sorted_accounts" / folder_key
    txt_file = fdir / spec["filename"]
    entries: list[tuple[str, str]] = []
    if txt_file.is_file():
        for raw_ln in txt_file.read_text(encoding="utf-8").splitlines():
            ln = raw_ln.strip()
            if not ln:
                continue
            head = ln.split("#", 1)[0].strip()
            if "|" in head:
                aid = head.split("|", 1)[0].strip().lstrip("@")
            else:
                aid = head.split(":", 1)[0].strip().lstrip("@")
            entries.append((aid, ln))
    return entries


def build_accounts_copy_payload(
    store: StateStore,
    state_dir: Path,
    *,
    scope: str = "uploaded",
    fmt: str = "lines",
    ids: list[str] | None = None,
    audit_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Builds instant clipboard-ready copy output for uploaded accounts, sorted folders,
    selected fleet accounts, or the entire fleet in multiple formats:
      - `lines`: original credential lines (`login:pass:outlook:...` / `user|pass|email|totp`)
      - `usernames`: `@handle` per line
      - `urls`: published post URLs (`https://www.tiktok.com/@handle/...`)
      - `summary`: `@handle | posts=N | views=M | folder/health | credential_line`
    """
    audit_map = audit_cache or {}
    all_accs = store.list_accounts()
    acc_by_id: dict[str, AccountRecord] = {a.account_id: a for a in all_accs}
    acc_by_low: dict[str, AccountRecord] = {}
    for a in all_accs:
        acc_by_low[a.account_id.lower()] = a
        if a.username:
            acc_by_low[a.username.lower()] = a

    rep_order, rep_by_id = _load_last_campaign_report_map(state_dir)

    # Map account_id -> folder_key from sorted_accounts folders
    folder_by_low: dict[str, str] = {}
    for fkey in SORTED_FOLDER_SPECS:
        for aid_key, _ in _load_sorted_folder_entries(state_dir, fkey):
            folder_by_low[aid_key.lower()] = fkey

    selected_pairs: list[tuple[AccountRecord | None, str, str]] = []  # (acc, fallback_id, fallback_line)
    seen_low: set[str] = set()

    def _add_acc(acc: AccountRecord | None, fid: str = "", fline: str = "") -> None:
        key = (acc.account_id if acc else fid).strip().lower()
        if not key or key in seen_low:
            return
        seen_low.add(key)
        selected_pairs.append((acc, acc.account_id if acc else fid, fline))

    if scope == "selected":
        for req_id in ids or []:
            acc = acc_by_id.get(req_id) or acc_by_low.get(req_id.lower())
            if acc is not None:
                _add_acc(acc)
    elif scope == "uploaded":
        # 1) Accounts with published posts from the latest campaign report (preserves batch order)
        for aid in rep_order:
            rep = rep_by_id[aid]
            if int(rep.get("posts_published") or 0) > 0:
                acc = acc_by_id.get(aid) or acc_by_low.get(aid.lower())
                _add_acc(acc, fid=aid, fline=aid)
        # 2) Any account in store with upload_history (e.g. uploaded via 1-post studio, CLI, or earlier batch)
        for acc in all_accs:
            if acc.upload_history:
                _add_acc(acc)
    elif scope in SORTED_FOLDER_SPECS:
        entries = _load_sorted_folder_entries(state_dir, scope)
        for aid_key, raw_ln in entries:
            acc = acc_by_low.get(aid_key.lower())
            _add_acc(acc, fid=aid_key, fline=raw_ln)
        # Convenient fallback for `clean` if sorted folder is still empty: pick audited eligible accounts
        if scope == "clean" and not selected_pairs:
            for acc in all_accs:
                aud = audit_map.get(acc.account_id) or {}
                if aud.get("account_health") == "eligible":
                    _add_acc(acc)
    else:  # "all"
        for acc in all_accs:
            _add_acc(acc)

    items: list[dict[str, Any]] = []
    out_lines: list[str] = []
    seen_urls: set[str] = set()

    for acc, fid, fline in selected_pairs:
        aid = acc.account_id if acc else fid
        aud = audit_map.get(aid) or {}
        rep = rep_by_id.get(aid) or {}
        handle = (
            str(aud.get("uniq_id") or "")
            or str(rep.get("username") or "")
            or (acc.username if acc else "")
            or aid
        ).strip().lstrip("@")

        copy_line = format_account_copy_line(acc) if acc is not None else fline

        # Collect published post URLs (report + upload_history)
        urls: list[str] = []
        for u in rep.get("post_urls") or []:
            if isinstance(u, str) and u.strip() and u not in urls:
                urls.append(u.strip())
        if acc is not None:
            for rec in acc.upload_history:
                if rec.aweme_id:
                    seg = "video" if rec.media_type == "video" else "photo"
                    u = f"https://www.tiktok.com/@{handle}/{seg}/{rec.aweme_id}"
                    if u not in urls:
                        urls.append(u)

        posts_cnt = max(
            int(rep.get("posts_published") or 0),
            len(acc.upload_history) if acc else 0,
            int(aud.get("total_posts") or 0),
        )
        views_cnt = int(aud.get("total_views") or rep.get("initial_views") or 0)
        folder_key = (
            folder_by_low.get(aid.lower())
            or folder_by_low.get(handle.lower())
            or str(rep.get("folder_key") or "")
        )
        health = str(aud.get("account_health") or ("eligible" if folder_key == "clean" else "unknown"))

        items.append(
            {
                "account_id": aid,
                "username": handle,
                "posts_count": posts_cnt,
                "views": views_cnt,
                "health": health,
                "folder_key": folder_key,
                "copy_line": copy_line,
                "post_urls": urls,
            }
        )

        if fmt == "usernames":
            out_lines.append(f"@{handle}")
        elif fmt == "urls":
            for u in urls:
                if u not in seen_urls:
                    seen_urls.add(u)
                    out_lines.append(u)
        elif fmt == "summary":
            tag = folder_key or health
            out_lines.append(f"@{handle} | posts={posts_cnt} | views={views_cnt} | {tag} | {copy_line}")
        else:  # "lines"
            out_lines.append(copy_line)

    return {
        "ok": True,
        "scope": scope,
        "fmt": fmt,
        "count": len(out_lines),
        "accounts_count": len(items),
        "text": "\n".join(out_lines),
        "items": items,
    }


# ── Standalone & Trailing-Thread 2048 Unban Wave Queue Manager ───────────────


class UnbanWaveQueueManager:
    """
    Dedicated multi-thread / sequential 2048 restriction wave-catcher queue.

    Can run in two ways:
      1. Standalone (`⚡ Снятие огранов` tab): processes selected, uploaded, or all authenticated
         accounts in `threads=1` (strict sequential) or `threads=2..10` (parallel trailing threads),
         waiting out every asynchronous 0..16+ restriction wave (`quiet_seconds` / `max_wait_seconds`),
         confirming auto-approval, and sorting into `state/sorted_accounts/`.
      2. Decoupled trailing worker pool for mass campaigns (`CampaignConfig.defer_post_appeal_to_queue=True`):
         as soon as an upload thread finishes posting on an account, it enqueues the account here
         and immediately starts uploading the next account while trailing threads finish the 2048 waves.
    """

    def __init__(
        self,
        store: StateStore,
        state_dir: Path,
        *,
        on_event: Callable[[str, str, str], None] | None = None,
        on_audit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self.store = store
        self.state_dir = Path(state_dir)
        self._on_event = on_event
        self._on_audit = on_audit

        self._queue: asyncio.Queue[tuple[str, float, bool]] = asyncio.Queue()
        self._queued_set: set[str] = set()
        self._workers: list[asyncio.Task[None]] = []
        self._delayed_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_event = asyncio.Event()
        self._records: dict[str, dict[str, Any]] = {}
        self._running = False
        self._config: dict[str, Any] = {
            "threads": 3,
            "wait_settle": True,
            "quiet_seconds": 60.0,
            "max_wait_seconds": 240.0,
            "poll_interval_seconds": 6.0,
            "only_account_ineligible": True,
            "appeal_studio_posts": False,
            "hide_rejected": True,
            "sort_to_folders": True,
            "delayed_recheck_minutes": 0,
            "recheck_include_studio": True,
            "appeal_text": (
                "Our content is original, authentic, and strictly follows all TikTok Community Guidelines."
            ),
        }

    def _emit(self, text: str, level: str = "info", kind: str = "unban_queue") -> None:
        if self._on_event:
            try:
                self._on_event(text, level, kind)
            except Exception:
                pass

    def _update_last_campaign_report_folder(
        self, account_id: str, folder_key: str, folder_title: str, appeals_delta: int
    ) -> None:
        rep_file = self.state_dir / "last_campaign_report.json"
        if not rep_file.is_file():
            return
        try:
            data = json.loads(rep_file.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                return
            changed = False
            for row in data:
                if isinstance(row, dict) and str(row.get("account_id")) == account_id:
                    row["folder_key"] = folder_key
                    row["appeal_verdict"] = folder_title
                    row["appeals_sent"] = int(row.get("appeals_sent") or 0) + int(appeals_delta)
                    changed = True
            if changed:
                rep_file.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def enqueue(
        self,
        account_ids: list[str],
        *,
        threads: int | None = None,
        wait_settle: bool | None = None,
        quiet_seconds: float | None = None,
        max_wait_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
        only_account_ineligible: bool | None = None,
        appeal_studio_posts: bool | None = None,
        hide_rejected: bool | None = None,
        sort_to_folders: bool | None = None,
        appeal_text: str | None = None,
        delayed_recheck_minutes: int | None = None,
        recheck_include_studio: bool | None = None,
        initial_wait_sec: float = 0.0,
        clear_finished: bool = False,
        is_recheck: bool = False,
    ) -> dict[str, Any]:
        # Prune completed worker handles
        self._workers = [w for w in self._workers if not w.done()]
        if clear_finished and not self._workers:
            self._records.clear()

        if threads is not None:
            self._config["threads"] = max(1, min(15, int(threads)))
        if wait_settle is not None:
            self._config["wait_settle"] = bool(wait_settle)
        if quiet_seconds is not None:
            self._config["quiet_seconds"] = max(0.0, float(quiet_seconds))
        if max_wait_seconds is not None:
            self._config["max_wait_seconds"] = max(10.0, float(max_wait_seconds))
        if poll_interval_seconds is not None:
            self._config["poll_interval_seconds"] = max(0.05, float(poll_interval_seconds))
        if only_account_ineligible is not None:
            self._config["only_account_ineligible"] = bool(only_account_ineligible)
        if appeal_studio_posts is not None:
            self._config["appeal_studio_posts"] = bool(appeal_studio_posts)
        if hide_rejected is not None:
            self._config["hide_rejected"] = bool(hide_rejected)
        if sort_to_folders is not None:
            self._config["sort_to_folders"] = bool(sort_to_folders)
        if delayed_recheck_minutes is not None:
            self._config["delayed_recheck_minutes"] = max(0, min(120, int(delayed_recheck_minutes)))
        if recheck_include_studio is not None:
            self._config["recheck_include_studio"] = bool(recheck_include_studio)
        if appeal_text is not None and appeal_text.strip():
            self._config["appeal_text"] = appeal_text.strip()

        self._stop_event.clear()
        added: list[str] = []
        skipped: list[str] = []

        for aid in account_ids:
            aid_s = str(aid or "").strip()
            if not aid_s or aid_s in self._queued_set:
                continue
            try:
                acc = self.store.load_account(aid_s)
            except Exception:
                skipped.append(aid_s)
                continue
            if not acc.authenticated or not acc.device:
                skipped.append(aid_s)
                continue
            self._queued_set.add(aid_s)
            self._records[aid_s] = {
                "account_id": aid_s,
                "username": acc.username or aid_s,
                "status": "queued",
                "appeals_sent": 0,
                "active_remaining": 0,
                "reviewing_count": 0,
                "idle_sec": 0,
                "quiet_sec": int(self._config["quiet_seconds"]),
                "elapsed_sec": 0,
                "folder_key": "",
                "folder_title": "Повторный чек (поздняя волна)..." if is_recheck else "В очереди...",
                "error": "",
                "copy_line": format_account_copy_line(acc),
                "updated_at": datetime.now().strftime("%H:%M:%S"),
            }
            self._queue.put_nowait((aid_s, max(0.0, float(initial_wait_sec)), bool(is_recheck)))
            added.append(aid_s)

        if added:
            self._running = True
            target_workers = min(int(self._config["threads"]), self._queue.qsize() + len(self._workers))
            while len(self._workers) < target_workers:
                worker_idx = len(self._workers) + 1
                self._workers.append(asyncio.create_task(self._worker_loop(worker_idx)))
            mode_lbl = (
                f"последовательно (1 поток)"
                if int(self._config["threads"]) == 1
                else f"{self._config['threads']} потоков дожима"
            )
            self._emit(
                f"⚡ Очередь снятия огранов 2048: +{len(added)} акк. ({mode_lbl}, "
                f"{'ожидание волн ' + str(int(self._config['quiet_seconds'])) + 'с' if self._config['wait_settle'] else 'быстрый 1 проход'})",
                "info",
                "unban_queue",
            )

        return {"added": added, "skipped": skipped, "snapshot": self.snapshot()}

    def stop(self) -> dict[str, Any]:
        self._stop_event.set()
        for t in list(self._delayed_tasks.values()):
            if not t.done():
                t.cancel()
        self._delayed_tasks.clear()
        drained = 0
        while not self._queue.empty():
            try:
                aid, _, _ = self._queue.get_nowait()
                self._queued_set.discard(aid)
                if aid in self._records and self._records[aid]["status"] == "queued":
                    self._records[aid]["status"] = "stopped"
                    self._records[aid]["folder_title"] = "Остановлено"
                    self._records[aid]["updated_at"] = datetime.now().strftime("%H:%M:%S")
                self._queue.task_done()
                drained += 1
            except asyncio.QueueEmpty:
                break
        self._emit(f"⏹ Остановка очереди снятия огранов 2048 (снято из очереди: {drained})...", "warn", "unban_queue")
        return {"ok": True, "drained": drained, "snapshot": self.snapshot()}

    def _schedule_delayed_recheck(self, aid: str, delay_minutes: int, include_studio: bool) -> None:
        old = self._delayed_tasks.get(aid)
        if old and not old.done():
            old.cancel()

        async def _later() -> None:
            try:
                if await self._interruptible_sleep(delay_minutes * 60.0):
                    return
                self._emit(
                    f"[{aid}] 🔁 Авто-дожим через {delay_minutes} мин: проверка поздних волн 2048"
                    + (" + Studio UNORIGINAL_NR" if include_studio else "") + "...",
                    "info",
                    "unban_queue",
                )
                self.enqueue(
                    [aid],
                    appeal_studio_posts=include_studio,
                    wait_settle=False,
                    clear_finished=False,
                    is_recheck=True,
                )
            except asyncio.CancelledError:
                return
            finally:
                self._delayed_tasks.pop(aid, None)

        self._delayed_tasks[aid] = asyncio.create_task(_later())

    def clear_finished(self) -> dict[str, Any]:
        for aid in list(self._records.keys()):
            if self._records[aid]["status"] in ("done", "error", "stopped"):
                del self._records[aid]
        self._emit("", "info", "unban_queue")
        return self.snapshot()

    async def _interruptible_sleep(self, seconds: float) -> bool:
        """Sleeps up to `seconds`, returning True immediately if stop_event was set."""
        slept = 0.0
        while slept < seconds:
            if self._stop_event.is_set():
                return True
            step = min(0.5, seconds - slept)
            await asyncio.sleep(step)
            slept += step
        return self._stop_event.is_set()

    async def _worker_loop(self, worker_id: int) -> None:
        try:
            while not self._stop_event.is_set():
                try:
                    aid, init_wait, is_recheck = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

                rec = self._records.get(aid)
                try:
                    if rec is not None:
                        rec["status"] = "waiting"
                        rec["folder_title"] = (
                            f"Поток #{worker_id}: пауза {int(init_wait)}с..."
                            if init_wait > 0
                            else f"Поток #{worker_id}: сканирование волн..."
                        )
                        rec["updated_at"] = datetime.now().strftime("%H:%M:%S")
                        self._emit("", "info", "unban_queue")

                    if init_wait > 0:
                        if await self._interruptible_sleep(init_wait):
                            if rec is not None:
                                rec["status"] = "stopped"
                                rec["folder_title"] = "Остановлено"
                            break

                    acc = self.store.load_account(aid)
                    cfg = dict(self._config)

                    def _progress(info: dict[str, Any]) -> None:
                        if rec is None:
                            return
                        rec["appeals_sent"] = int(info.get("appeals_sent") or 0)
                        rec["active_remaining"] = int(info.get("active_remaining") or 0)
                        rec["reviewing_count"] = int(info.get("reviewing_count") or 0)
                        rec["idle_sec"] = int(info.get("idle_sec") or 0)
                        rec["quiet_sec"] = int(info.get("quiet_sec") or 0)
                        rec["elapsed_sec"] = int(info.get("elapsed_sec") or 0)
                        rec["folder_title"] = (
                            f"Поток #{worker_id}: тишина {rec['idle_sec']}/{rec['quiet_sec']}с"
                            + (f" · акт:{rec['active_remaining']}" if rec["active_remaining"] else "")
                        )
                        rec["updated_at"] = datetime.now().strftime("%H:%M:%S")
                        self._emit("", "info", "unban_queue")

                    if cfg["wait_settle"]:
                        summary = await wait_and_appeal_settle(
                            acc,
                            appeal_text=cfg["appeal_text"],
                            appeal_studio_posts=cfg["appeal_studio_posts"],
                            only_account_ineligible=cfg["only_account_ineligible"],
                            verify_proxy_trace=False,
                            hide_rejected=cfg["hide_rejected"],
                            quiet_seconds=cfg["quiet_seconds"],
                            max_wait_seconds=cfg["max_wait_seconds"],
                            poll_interval_seconds=cfg["poll_interval_seconds"],
                            progress_cb=_progress,
                            stop_check=self._stop_event.is_set,
                        )
                    else:
                        summary = await remove_account_restrictions(
                            acc,
                            appeal_text=cfg["appeal_text"],
                            appeal_studio_posts=cfg["appeal_studio_posts"],
                            only_account_ineligible=cfg["only_account_ineligible"],
                            verify_proxy_trace=False,
                            hide_rejected=cfg["hide_rejected"],
                        )

                    self.store.save_account(acc)
                    if summary.audit and self._on_audit:
                        ad = asdict(summary.audit)
                        ad["audited_at"] = datetime.now().strftime("%H:%M:%S")
                        self._on_audit(aid, ad)

                    sent_total = summary.notice_appeals_verified + summary.post_appeals_verified
                    if self._stop_event.is_set():
                        if rec is not None:
                            rec["status"] = "stopped"
                            rec["appeals_sent"] = sent_total
                            rec["folder_title"] = "Остановлено"
                            rec["updated_at"] = datetime.now().strftime("%H:%M:%S")
                    else:
                        fkey, ftitle = classify_appeal_folder(summary.audit, had_error=False)
                        if cfg["sort_to_folders"]:
                            sort_account_into_folder(self.state_dir, fkey, acc)
                        self._update_last_campaign_report_folder(aid, fkey, ftitle, sent_total)
                        if rec is not None:
                            rec["status"] = "done"
                            rec["appeals_sent"] = sent_total
                            rec["active_remaining"] = (
                                sum(1 for v in summary.audit.notice_violations if v.can_appeal or v.status_code == 1)
                                if summary.audit
                                else 0
                            )
                            rec["reviewing_count"] = (
                                sum(1 for v in summary.audit.notice_violations if v.status_code == 4)
                                if summary.audit
                                else 0
                            )
                            rec["folder_key"] = fkey
                            rec["folder_title"] = ftitle
                            rec["copy_line"] = format_account_copy_line(acc)
                            rec["updated_at"] = datetime.now().strftime("%H:%M:%S")
                        self._emit(
                            f"[{aid}] 🛡️ Дожим 2048 завершён (поток #{worker_id}): подано={sent_total}, папка={fkey} ({ftitle})",
                            "success" if fkey == "clean" else "warn",
                            "audit_updated",
                        )
                        recheck_min = int(cfg.get("delayed_recheck_minutes") or 0)
                        if not is_recheck and recheck_min > 0 and not self._stop_event.is_set():
                            self._schedule_delayed_recheck(
                                aid, recheck_min, bool(cfg.get("recheck_include_studio", True))
                            )
                except Exception as exc:
                    if rec is not None:
                        rec["status"] = "error"
                        rec["error"] = str(exc)
                        rec["folder_key"] = "errors"
                        rec["folder_title"] = f"Ошибка: {str(exc)[:80]}"
                        rec["updated_at"] = datetime.now().strftime("%H:%M:%S")
                    self._emit(f"[{aid}] ❌ Ошибка очереди снятия огранов: {exc}", "error", "unban_queue")
                finally:
                    self._queued_set.discard(aid)
                    self._queue.task_done()
                    self._emit("", "info", "unban_queue")
        finally:
            # Check if this was the last running worker
            other_alive = [w for w in self._workers if w is not asyncio.current_task() and not w.done()]
            if not other_alive and self._queue.empty():
                self._running = False
                self._emit("", "info", "unban_queue")

    def snapshot(self) -> dict[str, Any]:
        items = list(self._records.values())
        counts = {
            "total": len(items),
            "queued": sum(1 for x in items if x["status"] == "queued"),
            "waiting": sum(1 for x in items if x["status"] == "waiting"),
            "done": sum(1 for x in items if x["status"] == "done"),
            "clean": sum(1 for x in items if x["folder_key"] == "clean"),
            "reviewing": sum(1 for x in items if x["folder_key"] == "ogran_na_proverke"),
            "restricted": sum(1 for x in items if x["folder_key"] == "ogran_ne_snyat"),
            "error": sum(1 for x in items if x["status"] == "error"),
            "stopped": sum(1 for x in items if x["status"] == "stopped"),
            "appeals_total": sum(int(x.get("appeals_sent") or 0) for x in items),
        }
        active_workers = sum(1 for w in self._workers if not w.done())
        scheduled_rechecks = sum(1 for t in self._delayed_tasks.values() if not t.done())
        return {
            "ok": True,
            "running": self._running or active_workers > 0,
            "stopping": self._stop_event.is_set(),
            "active_workers": active_workers,
            "scheduled_rechecks": scheduled_rechecks,
            "config": dict(self._config),
            "counts": counts,
            "items": items,
        }

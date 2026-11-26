"""
Goal-Driven Smart TikTok Autopilot ("Автопилот 600").

End-to-end autonomous production loop per account:
  1. Upload Wave (N parallel upload threads):
     - Anti-shadowban campaign (`anti_unoriginal` / `minimal_snow` uniquifier, randomized tags,
       unique music track per post, inter-post 2048 Auto-Appeal, cold TikTok restart every 3 posts).
     - Immediately releases the upload slot once posts are published so throughput stays maxed.
  2. Post-Upload 2048 Wave Catcher (dedicated parallel appeal threads):
     - Catches asynchronous 0..16+ 2048 restriction waves via pure-HTTP `wait_and_appeal_settle`.
  3. Zero-Cost Live View & Moderation Monitor (pure-HTTP cookies, $0 Kintaro cost):
     - Polls real views + IDC 2048 + TikTok Studio penalties every `monitor_interval_sec`.
     - Automatically appeals any late 2048 notices and Studio `UNORIGINAL_NR` flags.
     - Marks account `goal_reached` as soon as it crosses `target_views` (default 600) and
       preserves winning posts untouched + sorts into `state/sorted_accounts/clean/`.
  4. Smart Adaptation (Growth Patience vs Non-Destructive Top-Up vs Clean Re-Pour):
     - Actively climbing (`velocity >= growth_extend_views`): extends patience (+10m) automatically.
     - Live-but-stalled (`views >= protect_views_threshold`): keeps live posts (`delete_old=False`),
       does a cold restart, and tops up with `+topup_copies` fresh uniquified posts to push past 600.
     - Shadowed / dead (`views < protect_views_threshold`): cleans dead posts, cold-restarts TikTok,
       rotates uniquifier preset, and re-pours a fresh full round (up to `max_rounds`).
  5. Continuous Pool Refill:
     - Whenever an account finishes, automatically pulls the next eligible clean account from the pool.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from kintaro_auto.appeals import (
    AccountAppealSummary,
    AccountAuditResult,
    audit_account_restrictions,
    classify_appeal_folder,
    remove_account_restrictions,
    sort_account_into_folder,
    wait_and_appeal_settle,
)
from kintaro_auto.client import KintaroClient
from kintaro_auto.config import AppConfig
from kintaro_auto.models import AccountRecord, CampaignAccountResult, CampaignConfig
from kintaro_auto.pipeline import AutomationPipeline
from kintaro_auto.response_adapters import extract_feed_items, extract_play_count
from kintaro_auto.storage import StateStore, format_account_copy_line

logger = logging.getLogger("kintaro_auto")

_PHOTO_MODE_ROTATION: dict[str, list[str]] = {
    "anti_unoriginal": ["anti_unoriginal", "minimal_snow", "boosted"],
    "minimal_snow": ["minimal_snow", "anti_unoriginal", "boosted"],
    "boosted": ["boosted", "anti_unoriginal", "minimal_snow"],
    "stealth": ["stealth", "anti_unoriginal", "minimal_snow"],
    "clean_crop": ["clean_crop", "anti_unoriginal", "minimal_snow"],
}


@dataclass
class GoalAutopilotConfig:
    target_views: int = 600
    target_mode: str = "total"  # "total" | "max_post"
    media_files: list[str] = field(default_factory=list)
    description: str = "#piratetoken #piratetokenshop #blackknight #pirateshop #fyp"
    auto_music_query: str = "Fortnite"
    photo_mode: str = "anti_unoriginal"
    copies_per_round: int = 6
    topup_copies: int = 3
    protect_views_threshold: int = 150
    max_rounds: int = 3
    threads: int = 3
    appeal_threads: int = 3
    monitor_interval_sec: int = 90
    reupload_wait_minutes: int = 25
    growth_extend_views: int = 35
    max_patience_minutes: int = 55
    region: str = "US"
    app_locale: str = "en_US"
    restart_every_n: int = 3
    delay_min_sec: float = 14.0
    delay_max_sec: float = 20.0
    randomize_tags: bool = True
    pin_sticky_proxy: bool = True
    override_proxy_country: str = ""
    auto_appeal_2048: bool = True
    auto_appeal_studio: bool = True
    hide_rejected: bool = True
    delete_old_on_first_round: bool = False
    scope: str = "clean_empty"  # "clean_empty" | "clean" | "selected" | "all_authed"
    account_ids: list[str] = field(default_factory=list)
    max_accounts: int = 0  # 0 = unlimited (all eligible in scope)
    auto_refill_pool: bool = True
    appeal_quiet_seconds: float = 45.0
    appeal_max_wait_seconds: float = 180.0
    appeal_text: str = (
        "Our content is original, authentic, and strictly follows all TikTok Community Guidelines."
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def normalized(self) -> GoalAutopilotConfig:
        d = self.to_dict()
        d["target_views"] = max(50, min(500_000, int(d.get("target_views") or 600)))
        d["target_mode"] = "max_post" if d.get("target_mode") == "max_post" else "total"
        d["copies_per_round"] = max(1, min(12, int(d.get("copies_per_round") or 6)))
        d["topup_copies"] = max(1, min(d["copies_per_round"], int(d.get("topup_copies") or 3)))
        d["protect_views_threshold"] = max(0, min(50_000, int(d.get("protect_views_threshold") or 0)))
        d["max_rounds"] = max(1, min(6, int(d.get("max_rounds") or 3)))
        d["threads"] = max(1, min(10, int(d.get("threads") or 3)))
        d["appeal_threads"] = max(1, min(10, int(d.get("appeal_threads") or 3)))
        d["monitor_interval_sec"] = max(15, min(600, int(d.get("monitor_interval_sec") or 90)))
        d["reupload_wait_minutes"] = max(2, min(180, int(d.get("reupload_wait_minutes") or 25)))
        d["growth_extend_views"] = max(5, min(10_000, int(d.get("growth_extend_views") or 35)))
        d["max_patience_minutes"] = max(
            d["reupload_wait_minutes"], min(240, int(d.get("max_patience_minutes") or 55))
        )
        d["restart_every_n"] = max(0, min(12, int(d.get("restart_every_n") or 0)))
        d["delay_min_sec"] = max(0.0, min(120.0, float(d.get("delay_min_sec") or 0.0)))
        d["delay_max_sec"] = max(d["delay_min_sec"], min(180.0, float(d.get("delay_max_sec") or 0.0)))
        d["max_accounts"] = max(0, min(500, int(d.get("max_accounts") or 0)))
        d["appeal_quiet_seconds"] = max(0.0, min(240.0, float(d.get("appeal_quiet_seconds") or 0.0)))
        d["appeal_max_wait_seconds"] = max(
            max(15.0, d["appeal_quiet_seconds"]), min(600.0, float(d.get("appeal_max_wait_seconds") or 180.0))
        )
        d["region"] = (str(d.get("region") or "US").strip().upper() or "US")[:4]
        d["app_locale"] = str(d.get("app_locale") or "en_US").strip() or "en_US"
        d["override_proxy_country"] = str(d.get("override_proxy_country") or "").strip().upper()[:4]
        if d.get("scope") not in ("clean_empty", "clean", "selected", "all_authed"):
            d["scope"] = "clean_empty"
        return GoalAutopilotConfig(**d)


AUTOPILOT_PRESETS: list[dict[str, Any]] = [
    {
        "id": "standard_600",
        "name": "🎯 Стандарт 600",
        "desc": "Сбалансированный залив до 600 просм. · 3 потока · умный долив +3 при ≥150",
        "values": {
            "target_views": 600,
            "target_mode": "total",
            "photo_mode": "anti_unoriginal",
            "copies_per_round": 6,
            "topup_copies": 3,
            "protect_views_threshold": 150,
            "max_rounds": 3,
            "threads": 3,
            "appeal_threads": 3,
            "monitor_interval_sec": 90,
            "reupload_wait_minutes": 25,
            "growth_extend_views": 35,
            "max_patience_minutes": 55,
            "restart_every_n": 3,
            "delay_min_sec": 14.0,
            "delay_max_sec": 20.0,
            "appeal_quiet_seconds": 45.0,
            "appeal_max_wait_seconds": 180.0,
            "auto_appeal_2048": True,
            "auto_appeal_studio": True,
            "hide_rejected": True,
            "auto_refill_pool": True,
        },
    },
    {
        "id": "turbo_600",
        "name": "⚡ Турбо 600",
        "desc": "Быстрый пролив в 5 потоков · короткие паузы 10–15с · 4 потока дожима 2048",
        "values": {
            "target_views": 600,
            "target_mode": "total",
            "photo_mode": "anti_unoriginal",
            "copies_per_round": 6,
            "topup_copies": 3,
            "protect_views_threshold": 140,
            "max_rounds": 3,
            "threads": 5,
            "appeal_threads": 4,
            "monitor_interval_sec": 75,
            "reupload_wait_minutes": 20,
            "growth_extend_views": 40,
            "max_patience_minutes": 45,
            "restart_every_n": 3,
            "delay_min_sec": 10.0,
            "delay_max_sec": 15.0,
            "appeal_quiet_seconds": 40.0,
            "appeal_max_wait_seconds": 150.0,
            "auto_appeal_2048": True,
            "auto_appeal_studio": True,
            "hide_rejected": True,
            "auto_refill_pool": True,
        },
    },
    {
        "id": "night_safe_600",
        "name": "🌙 Ночной мягкий",
        "desc": "Щадящий ночной темп · 2 потока · паузы 18–26с · терпение до 65 мин",
        "values": {
            "target_views": 600,
            "target_mode": "total",
            "photo_mode": "anti_unoriginal",
            "copies_per_round": 6,
            "topup_copies": 3,
            "protect_views_threshold": 120,
            "max_rounds": 3,
            "threads": 2,
            "appeal_threads": 3,
            "monitor_interval_sec": 110,
            "reupload_wait_minutes": 30,
            "growth_extend_views": 30,
            "max_patience_minutes": 65,
            "restart_every_n": 3,
            "delay_min_sec": 18.0,
            "delay_max_sec": 26.0,
            "appeal_quiet_seconds": 55.0,
            "appeal_max_wait_seconds": 210.0,
            "auto_appeal_2048": True,
            "auto_appeal_studio": True,
            "hide_rejected": True,
            "auto_refill_pool": True,
        },
    },
    {
        "id": "viral_post_1000",
        "name": "🔥 1 пост ≥ 1000",
        "desc": "Цель: хотя бы один пост на аккаунте ≥ 1000 просм. · до 4 раундов",
        "values": {
            "target_views": 1000,
            "target_mode": "max_post",
            "photo_mode": "anti_unoriginal",
            "copies_per_round": 6,
            "topup_copies": 4,
            "protect_views_threshold": 200,
            "max_rounds": 4,
            "threads": 3,
            "appeal_threads": 3,
            "monitor_interval_sec": 90,
            "reupload_wait_minutes": 30,
            "growth_extend_views": 50,
            "max_patience_minutes": 70,
            "restart_every_n": 3,
            "delay_min_sec": 14.0,
            "delay_max_sec": 20.0,
            "appeal_quiet_seconds": 45.0,
            "appeal_max_wait_seconds": 180.0,
            "auto_appeal_2048": True,
            "auto_appeal_studio": True,
            "hide_rejected": True,
            "auto_refill_pool": True,
        },
    },
]


def _now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _resolve_media_paths(state_dir: Path, media_files: list[str]) -> list[str]:
    mdir = Path(state_dir) / "media"
    resolved: list[str] = []
    for raw in media_files:
        s = str(raw or "").strip()
        if not s:
            continue
        p = Path(s)
        if p.is_file():
            resolved.append(str(p))
            continue
        cand = mdir / p.name
        if cand.is_file():
            resolved.append(str(cand))
    return resolved


def _pick_mode_for_round(base_mode: str, round_num: int) -> str:
    seq = _PHOTO_MODE_ROTATION.get(base_mode, [base_mode, "anti_unoriginal", "minimal_snow"])
    return seq[(max(1, round_num) - 1) % len(seq)]


class GoalAutopilotManager:
    """
    Production-grade autonomous 600-views TikTok Autopilot.
    Persists state to `state/autopilot_state.json`, emits live SSE updates,
    and coordinates cleanly with DashboardHub Watchdog via busy/managed account sets.
    """

    def __init__(
        self,
        get_cfg: Callable[[], AppConfig],
        get_store: Callable[[], StateStore],
        get_audit_cache: Callable[[], dict[str, dict[str, Any]]],
        *,
        on_event: Callable[[str, str, str], None] | None = None,
        on_audit: Callable[[str, dict[str, Any]], None] | None = None,
        busy_upload_accounts: set[str] | None = None,
        managed_accounts: set[str] | None = None,
        upload_round_fn: Callable[[AccountRecord, CampaignConfig, Callable[[str, str, dict[str, Any]], None]], Awaitable[CampaignAccountResult]] | None = None,
        appeal_fn: Callable[[AccountRecord, GoalAutopilotConfig, Callable[[], bool]], Awaitable[AccountAppealSummary]] | None = None,
        monitor_fn: Callable[[AccountRecord, GoalAutopilotConfig], Awaitable[dict[str, Any]]] | None = None,
        sleep_fn: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._get_cfg = get_cfg
        self._get_store = get_store
        self._get_audit_cache = get_audit_cache
        self._on_event = on_event
        self._on_audit = on_audit
        self._busy_upload = busy_upload_accounts if busy_upload_accounts is not None else set()
        self._managed = managed_accounts if managed_accounts is not None else set()

        self._upload_round_fn = upload_round_fn
        self._appeal_fn = appeal_fn
        self._monitor_fn = monitor_fn
        self._sleep_fn = sleep_fn

        self.run_id: str = ""
        self.started_at: str = ""
        self.started_ts: float = 0.0
        self.config: GoalAutopilotConfig = GoalAutopilotConfig()
        self.items: dict[str, dict[str, Any]] = {}
        self.running: bool = False
        self.stopping: bool = False

        self._stop_event = asyncio.Event()
        self._upload_sem: asyncio.Semaphore | None = None
        self._appeal_sem: asyncio.Semaphore | None = None
        self._account_tasks: dict[str, asyncio.Task[None]] = {}
        self._supervisor_task: asyncio.Task[None] | None = None

        self._load_persisted_state()
        self._load_saved_config()

    @property
    def _state_file(self) -> Path:
        return Path(self._get_cfg().state_dir) / "autopilot_state.json"

    @property
    def _config_file(self) -> Path:
        return Path(self._get_cfg().state_dir) / "autopilot_config.json"

    def _load_saved_config(self) -> None:
        fp = self._config_file
        if not fp.is_file():
            return
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                known = {f for f in GoalAutopilotConfig.__dataclass_fields__}
                merged = {**self.config.to_dict(), **{k: v for k, v in raw.items() if k in known}}
                self.config = GoalAutopilotConfig(**merged).normalized()
        except Exception:
            pass

    def save_config(self, updates: dict[str, Any] | GoalAutopilotConfig) -> dict[str, Any]:
        known = {f for f in GoalAutopilotConfig.__dataclass_fields__}
        if isinstance(updates, GoalAutopilotConfig):
            new_cfg = updates.normalized()
        else:
            merged = {**self.config.to_dict(), **{k: v for k, v in updates.items() if k in known and v is not None}}
            new_cfg = GoalAutopilotConfig(**merged).normalized()

        # Store short filenames for media inside state/media/ so config stays portable
        mdir = Path(self._get_cfg().state_dir) / "media"
        portable_media: list[str] = []
        for mf in new_cfg.media_files:
            p = Path(str(mf))
            if (mdir / p.name).is_file():
                portable_media.append(p.name)
            elif str(mf).strip():
                portable_media.append(str(mf).strip())

        self.config = new_cfg
        if self.running:
            # Hot-apply live target / max_rounds to active items immediately
            for it in self.items.values():
                if it["phase"] in ("queued", "uploading", "appealing", "monitoring"):
                    it["target_views"] = new_cfg.target_views
                    it["max_rounds"] = new_cfg.max_rounds
            self._save_persisted_state()

        try:
            fp = self._config_file
            fp.parent.mkdir(parents=True, exist_ok=True)
            disk_dict = new_cfg.to_dict()
            disk_dict["media_files"] = portable_media
            fp.write_text(json.dumps(disk_dict, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

        self._emit(
            f"💾 Настройки Автопилота сохранены (цель={new_cfg.target_views}, пот.={new_cfg.threads}, "
            f"постов={new_cfg.copies_per_round}, ожидание={new_cfg.reupload_wait_minutes}м)",
            "success",
            "autopilot",
        )
        return self.get_config_payload()

    def get_config_payload(self) -> dict[str, Any]:
        mdir = Path(self._get_cfg().state_dir) / "media"
        d = self.config.to_dict()
        d["media_files"] = [
            Path(str(m)).name if (mdir / Path(str(m)).name).is_file() else str(m)
            for m in (d.get("media_files") or [])
        ]
        return {
            "ok": True,
            "config": d,
            "presets": AUTOPILOT_PRESETS,
            "running": self.running,
        }

    def _emit(self, text: str, level: str = "info", kind: str = "autopilot") -> None:
        if self._on_event:
            try:
                self._on_event(text, level, kind)
            except Exception:
                pass

    def _save_persisted_state(self) -> None:
        try:
            fp = self._state_file
            fp.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "run_id": self.run_id,
                "started_at": self.started_at,
                "started_ts": self.started_ts,
                "config": self.config.to_dict(),
                "items": list(self.items.values()),
            }
            fp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _load_persisted_state(self) -> None:
        fp = self._state_file
        if not fp.is_file():
            return
        try:
            raw = json.loads(fp.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            self.run_id = str(raw.get("run_id") or "")
            self.started_at = str(raw.get("started_at") or "")
            self.started_ts = float(raw.get("started_ts") or 0.0)
            cfg_d = raw.get("config")
            if isinstance(cfg_d, dict):
                known = {f for f in GoalAutopilotConfig.__dataclass_fields__}
                self.config = GoalAutopilotConfig(**{k: v for k, v in cfg_d.items() if k in known})
            for it in raw.get("items") or []:
                if isinstance(it, dict) and it.get("account_id"):
                    # Any in-flight task from a previous server process is marked stopped on cold restart
                    if it.get("phase") in ("queued", "uploading", "appealing", "monitoring"):
                        it["phase"] = "stopped"
                        it["detail"] = "Прервано при перезапуске сервера"
                    self.items[str(it["account_id"])] = it
        except Exception:
            pass

    async def _sleep_interruptible(self, seconds: float) -> bool:
        if seconds <= 0:
            return self._stop_event.is_set()
        if self._sleep_fn is not None:
            await self._sleep_fn(seconds)
            return self._stop_event.is_set()
        slept = 0.0
        while slept < seconds:
            if self._stop_event.is_set():
                return True
            step = min(1.0, seconds - slept)
            await asyncio.sleep(step)
            slept += step
        return self._stop_event.is_set()

    def _select_candidate_accounts(self, cfg: GoalAutopilotConfig) -> list[AccountRecord]:
        store = self._get_store()
        audit_cache = self._get_audit_cache()
        all_accs = store.list_accounts()

        if cfg.account_ids:
            by_id = {a.account_id: a for a in all_accs}
            selected = [by_id[aid] for aid in cfg.account_ids if aid in by_id]
        elif cfg.scope == "clean_empty":
            selected = []
            for a in all_accs:
                if not (a.authenticated and a.device):
                    continue
                aud = audit_cache.get(a.account_id) or {}
                health = aud.get("account_health")
                posts = int(aud.get("total_posts") or 0)
                views = int(aud.get("total_views") or 0)
                if health in ("eligible", None, "") and posts == 0 and views == 0:
                    selected.append(a)
        elif cfg.scope == "clean":
            selected = [
                a
                for a in all_accs
                if a.authenticated
                and a.device
                and (audit_cache.get(a.account_id) or {}).get("account_health") == "eligible"
            ]
        else:  # "all_authed"
            selected = [a for a in all_accs if a.authenticated and a.device]

        # Prioritize authenticated accounts with 0 posts first, then lowest views
        def _sort_key(a: AccountRecord) -> tuple[int, int, int, str]:
            aud = audit_cache.get(a.account_id) or {}
            health = aud.get("account_health") or ""
            h_prio = 0 if health == "eligible" else (1 if not health else 2)
            posts = int(aud.get("total_posts") or 0)
            views = int(aud.get("total_views") or 0)
            return (h_prio, posts, views, a.account_id)

        selected = [a for a in selected if a.authenticated and a.device]
        selected.sort(key=_sort_key)
        return selected

    def _make_item(self, acc: AccountRecord, cfg: GoalAutopilotConfig) -> dict[str, Any]:
        aud = self._get_audit_cache().get(acc.account_id) or {}
        return {
            "account_id": acc.account_id,
            "username": aud.get("uniq_id") or acc.username or acc.account_id,
            "phase": "queued",  # queued | uploading | appealing | monitoring | goal_reached | capped | error | stopped
            "round_num": 0,
            "max_rounds": cfg.max_rounds,
            "next_round_topup": False,
            "posts_published": 0,
            "posts_live": int(aud.get("total_posts") or 0),
            "appeals_sent": 0,
            "views_total": int(aud.get("total_views") or 0),
            "best_post_views": 0,
            "target_views": cfg.target_views,
            "views_velocity": 0,
            "views_history": [],
            "post_urls": [],
            "health": aud.get("account_health") or "unknown",
            "folder_key": "",
            "detail": "В очереди на залив...",
            "error": "",
            "patience_extensions": 0,
            "last_round_finished_ts": 0.0,
            "updated_at": _now_hms(),
            "copy_line": format_account_copy_line(acc),
        }

    def start(self, cfg: GoalAutopilotConfig) -> dict[str, Any]:
        if self.running:
            raise RuntimeError("Автопилот уже запущен. Сначала остановите текущий прогон.")

        state_dir = Path(self._get_cfg().state_dir)
        media_paths = _resolve_media_paths(state_dir, cfg.media_files)
        if not media_paths:
            raise ValueError(
                "Не выбраны креативы для автопилота (загрузите фото/видео в библиотеку state/media/)."
            )

        cfg = cfg.normalized()
        cfg.media_files = media_paths
        # Persist user's chosen config cleanly before launching
        self.save_config(cfg)
        cfg.media_files = media_paths

        candidates = self._select_candidate_accounts(cfg)
        if cfg.max_accounts > 0:
            candidates = candidates[: cfg.max_accounts]
        if not candidates:
            raise RuntimeError(
                "Нет подходящих авторизованных аккаунтов для выбранного режима автопилота."
            )

        self.config = cfg
        self.run_id = uuid.uuid4().hex[:8]
        self.started_at = _now_hms()
        self.started_ts = time.time()
        self.items = {}
        self.running = True
        self.stopping = False
        self._stop_event.clear()
        self._upload_sem = asyncio.Semaphore(cfg.threads)
        self._appeal_sem = asyncio.Semaphore(cfg.appeal_threads)
        self._account_tasks.clear()

        for acc in candidates:
            self.items[acc.account_id] = self._make_item(acc, cfg)
            self._managed.add(acc.account_id)
            self._account_tasks[acc.account_id] = asyncio.create_task(
                self._run_account_lifecycle(acc.account_id)
            )

        self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        self._save_persisted_state()
        self._emit(
            f"🚀 Автопилот #{self.run_id} запущен: цель {cfg.target_views} просм. | "
            f"{len(candidates)} акк. | {cfg.threads} пот. залива | {cfg.copies_per_round} постов/раунд | "
            f"уник={cfg.photo_mode}",
            "success",
            "autopilot",
        )
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        if not self.running and not self.stopping:
            return self.snapshot()
        self.stopping = True
        self._stop_event.set()
        for t in list(self._account_tasks.values()):
            if not t.done():
                t.cancel()
        if self._supervisor_task and not self._supervisor_task.done():
            self._supervisor_task.cancel()
        for it in self.items.values():
            if it["phase"] in ("queued", "uploading", "appealing", "monitoring"):
                it["phase"] = "stopped"
                it["detail"] = "Остановлено пользователем"
                it["updated_at"] = _now_hms()
        self._managed.clear()
        self._busy_upload.clear()
        self.running = False
        self.stopping = False
        self._save_persisted_state()
        self._emit("⏹ Автопилот остановлен.", "warn", "autopilot")
        return self.snapshot()

    def clear(self) -> dict[str, Any]:
        if self.running:
            raise RuntimeError("Сначала остановите автопилот.")
        self.items.clear()
        self.run_id = ""
        self.started_at = ""
        self.started_ts = 0.0
        self._save_persisted_state()
        self._emit("", "info", "autopilot")
        return self.snapshot()

    async def _supervisor_loop(self) -> None:
        try:
            while not self._stop_event.is_set():
                # Prune finished tasks
                for aid in list(self._account_tasks.keys()):
                    if self._account_tasks[aid].done():
                        self._account_tasks.pop(aid, None)
                        self._managed.discard(aid)

                # Continuous pool refill if enabled and under max_accounts cap
                cfg = self.config
                if cfg.auto_refill_pool and not cfg.account_ids and not self._stop_event.is_set():
                    cap = cfg.max_accounts
                    under_cap = (cap == 0) or (len(self.items) < cap)
                    active_now = len(self._account_tasks)
                    if under_cap and active_now < max(cfg.threads * 2, cfg.threads + 2):
                        for cand in self._select_candidate_accounts(cfg):
                            if cand.account_id in self.items:
                                continue
                            if cap > 0 and len(self.items) >= cap:
                                break
                            self.items[cand.account_id] = self._make_item(cand, cfg)
                            self._managed.add(cand.account_id)
                            self._account_tasks[cand.account_id] = asyncio.create_task(
                                self._run_account_lifecycle(cand.account_id)
                            )
                            self._emit(
                                f"[{cand.account_id}] ➕ Автопилот подхватил свежий чистый аккаунт из пула",
                                "info",
                                "autopilot",
                            )
                            self._save_persisted_state()
                            if len(self._account_tasks) >= max(cfg.threads * 2, cfg.threads + 2):
                                break

                if not self._account_tasks:
                    break

                if await self._sleep_interruptible(5.0):
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self.running = False
            self.stopping = False
            self._managed.clear()
            self._save_persisted_state()
            snap = self.snapshot()
            c = snap["counts"]
            if c["total"] > 0 and not self._stop_event.is_set():
                self._emit(
                    f"🏁 Автопилот #{self.run_id} завершён: 🎯 600+={c['goal_reached']}/{c['total']} | "
                    f"всего просм.={c['total_views']} | постов={c['total_posts']} | апелляций={c['total_appeals']}",
                    "success",
                    "autopilot",
                )

    async def _default_upload_round(
        self,
        acc: AccountRecord,
        camp: CampaignConfig,
        progress_cb: Callable[[str, str, dict[str, Any]], None],
    ) -> CampaignAccountResult:
        cfg_app = self._get_cfg()
        store = self._get_store()
        async with KintaroClient(cfg_app) as client:
            pipeline = AutomationPipeline(cfg_app, store, client)
            results = await pipeline.run_anti_shadow_campaign(camp, progress_cb=progress_cb)
        return results[0]

    async def _default_appeal_wave(
        self,
        acc: AccountRecord,
        cfg: GoalAutopilotConfig,
        stop_check: Callable[[], bool],
    ) -> AccountAppealSummary:
        summary = await wait_and_appeal_settle(
            acc,
            appeal_text=cfg.appeal_text,
            appeal_studio_posts=False,
            only_account_ineligible=True,
            verify_proxy_trace=False,
            hide_rejected=cfg.hide_rejected,
            quiet_seconds=cfg.appeal_quiet_seconds,
            max_wait_seconds=cfg.appeal_max_wait_seconds,
            poll_interval_seconds=6.0,
            stop_check=stop_check,
        )
        self._get_store().save_account(acc)
        return summary

    async def _default_monitor_tick(
        self,
        acc: AccountRecord,
        cfg: GoalAutopilotConfig,
    ) -> dict[str, Any]:
        # Pure-HTTP $0 check: IDC 2048 + TikTok Studio posts & views + auto-appeal if any penalty appears
        summary = await remove_account_restrictions(
            acc,
            appeal_text=cfg.appeal_text,
            appeal_studio_posts=cfg.auto_appeal_studio,
            only_account_ineligible=False,
            verify_proxy_trace=False,
            hide_rejected=cfg.hide_rejected,
        )
        store = self._get_store()
        store.save_account(acc)

        aud: AccountAuditResult | None = summary.audit
        views_total = int(aud.total_views) if aud else 0
        posts_live = int(aud.total_posts) if aud else 0
        best_post = max((int(p.play_count or 0) for p in (aud.post_penalties if aud else [])), default=0)

        # If Studio returned 0 views while posts exist, verify via lightweight Kintaro get_self_posts once
        if views_total == 0 and (posts_live > 0 or len(acc.upload_history) > 0):
            try:
                cfg_app = self._get_cfg()
                async with KintaroClient(cfg_app) as client:
                    sp = await client.get_self_posts(acc, count=12)
                sp_items = extract_feed_items(sp, cfg_app.response_mapping)
                if sp_items:
                    plays = [int(extract_play_count(it, cfg_app.response_mapping) or 0) for it in sp_items]
                    views_total = max(views_total, sum(plays))
                    best_post = max(best_post, max(plays, default=0))
                    posts_live = max(posts_live, len(sp_items))
            except Exception:
                pass

        aud_dict: dict[str, Any] | None = None
        if aud is not None:
            aud_dict = asdict(aud)
            aud_dict["total_views"] = max(int(aud_dict.get("total_views") or 0), views_total)
            aud_dict["total_posts"] = max(int(aud_dict.get("total_posts") or 0), posts_live)
            aud_dict["audited_at"] = _now_hms()
            if self._on_audit:
                self._on_audit(acc.account_id, aud_dict)
            fkey, _ = classify_appeal_folder(aud)
            sort_account_into_folder(Path(self._get_cfg().state_dir), fkey, acc)
        else:
            fkey = ""

        return {
            "views_total": views_total,
            "best_post_views": best_post,
            "posts_live": posts_live,
            "appeals_sent": summary.notice_appeals_verified + summary.post_appeals_verified,
            "posts_hidden": summary.posts_hidden,
            "health": aud.account_health if aud else "unknown",
            "folder_key": fkey,
            "username": (aud.uniq_id if aud and aud.uniq_id else "") or acc.username or acc.account_id,
        }

    def _compute_velocity_10m(self, history: list[list[float | int]]) -> int:
        if len(history) < 2:
            return 0
        now_ts, now_v = history[-1][0], int(history[-1][1])
        ref_v = int(history[0][1])
        ref_ts = float(history[0][0])
        for ts, v in reversed(history[:-1]):
            ref_ts = float(ts)
            ref_v = int(v)
            if (now_ts - ref_ts) >= 540.0:  # ~9-10 min window
                break
        dt_min = max(1.0, (now_ts - ref_ts) / 60.0)
        delta = max(0, now_v - ref_v)
        return int(round((delta / dt_min) * 10.0))

    async def _run_account_lifecycle(self, account_id: str) -> None:
        cfg = self.config
        it = self.items[account_id]
        upload_sem = self._upload_sem or asyncio.Semaphore(cfg.threads)
        appeal_sem = self._appeal_sem or asyncio.Semaphore(cfg.appeal_threads)

        is_video = Path(cfg.media_files[0]).suffix.lower() == ".mp4"
        transient_retries = 0

        try:
            while not self._stop_event.is_set():
                next_round = int(it["round_num"]) + 1
                if next_round > cfg.max_rounds:
                    it["phase"] = "capped"
                    it["detail"] = f"Лимит {cfg.max_rounds} раундов ({it['views_total']}/{cfg.target_views})"
                    it["updated_at"] = _now_hms()
                    self._save_persisted_state()
                    self._emit(
                        f"[{account_id}] ⏸ Автопилот: лимит {cfg.max_rounds} раундов ({it['views_total']}/{cfg.target_views} просм.)",
                        "warn",
                        "autopilot",
                    )
                    return

                # ── PHASE 1: UPLOAD ROUND (protected by upload_sem & busy_upload set) ──
                it["phase"] = "queued"
                it["detail"] = (
                    f"В очереди на долив +{cfg.topup_copies} (раунд #{next_round}/{cfg.max_rounds})..."
                    if it.get("next_round_topup")
                    else f"В очереди на залив (раунд #{next_round}/{cfg.max_rounds})..."
                )
                it["updated_at"] = _now_hms()
                self._emit("", "info", "autopilot")

                async with upload_sem:
                    if self._stop_event.is_set():
                        return
                    self._busy_upload.add(account_id)
                    try:
                        acc = self._get_store().load_account(account_id)
                        is_topup = bool(it.get("next_round_topup"))
                        copies_now = cfg.topup_copies if is_topup else cfg.copies_per_round
                        delete_old = (
                            cfg.delete_old_on_first_round
                            if next_round == 1
                            else (not is_topup)
                        )
                        round_photo_mode = _pick_mode_for_round(cfg.photo_mode, next_round)

                        it["round_num"] = next_round
                        it["phase"] = "uploading"
                        it["detail"] = (
                            f"Долив +{copies_now} ({round_photo_mode})..."
                            if is_topup
                            else f"Залив 0/{copies_now} (раунд #{next_round}, {round_photo_mode})..."
                        )
                        it["updated_at"] = _now_hms()
                        self._save_persisted_state()
                        self._emit(
                            f"[{account_id}] ▶ Раунд #{next_round}/{cfg.max_rounds}: "
                            f"{'долив +' + str(copies_now) + ' (живые посты сохранены)' if is_topup else f'залив {copies_now} постов'} "
                            f"({round_photo_mode}, {'очистка' if delete_old else 'без удаления'})",
                            "info",
                            "autopilot",
                        )

                        camp = CampaignConfig(
                            account_ids=[account_id],
                            media_type="video" if is_video else "photo",
                            media_paths=list(cfg.media_files),
                            description=cfg.description,
                            randomize_tags=cfg.randomize_tags,
                            copies_count=copies_now,
                            threads=1,
                            delay_min_sec=cfg.delay_min_sec,
                            delay_max_sec=cfg.delay_max_sec,
                            restart_every_n=cfg.restart_every_n,
                            region=cfg.region,
                            phone_region=cfg.region,
                            app_locale=cfg.app_locale,
                            pin_sticky_proxy=cfg.pin_sticky_proxy,
                            override_proxy_country=cfg.override_proxy_country,
                            delete_old_posts=delete_old,
                            photo_mode=round_photo_mode,
                            auto_music_query=cfg.auto_music_query,
                            auto_appeal_2048=cfg.auto_appeal_2048,
                            appeal_pre_upload=cfg.auto_appeal_2048,
                            appeal_inter_post=cfg.auto_appeal_2048,
                            # Handled immediately in Phase 2 (`appeal_sem`) so upload slot frees up instantly!
                            appeal_post_upload=False,
                            defer_post_appeal_to_queue=False,
                            sort_to_folders=False,
                            appeal_text=cfg.appeal_text,
                        )

                        def _prog(_aid: str, msg: str, meta: dict[str, Any]) -> None:
                            pub = meta.get("published")
                            if pub is not None:
                                it["detail"] = f"Залив {pub}/{copies_now} (раунд #{next_round})"
                            else:
                                it["detail"] = msg[:72]
                            it["updated_at"] = _now_hms()
                            self._emit("", "info", "autopilot")

                        upload_fn = self._upload_round_fn or self._default_upload_round
                        res = await upload_fn(acc, camp, _prog)
                    finally:
                        self._busy_upload.discard(account_id)

                if self._stop_event.is_set():
                    return

                it["posts_published"] = int(it["posts_published"]) + int(res.posts_published)
                it["appeals_sent"] = int(it["appeals_sent"]) + int(res.appeals_sent)
                for u in res.post_urls or []:
                    if u and u not in it["post_urls"]:
                        it["post_urls"].append(u)
                it["username"] = res.username or it["username"]

                if res.posts_published == 0:
                    err_txt = res.error or "Не удалось опубликовать ни одного поста"
                    if transient_retries < 2 and not self._stop_event.is_set():
                        transient_retries += 1
                        it["round_num"] = max(0, int(it["round_num"]) - 1)
                        it["detail"] = f"Ретрай {transient_retries}/2 через 25с: {err_txt[:55]}"
                        it["updated_at"] = _now_hms()
                        self._emit(
                            f"[{account_id}] ⚠️ Сбой раунда ({err_txt[:80]}) — повтор {transient_retries}/2 через 25с",
                            "warn",
                            "autopilot",
                        )
                        if await self._sleep_interruptible(25.0):
                            return
                        continue
                    it["phase"] = "error"
                    it["error"] = err_txt
                    it["detail"] = f"Ошибка: {err_txt[:70]}"
                    it["updated_at"] = _now_hms()
                    self._save_persisted_state()
                    self._emit(f"[{account_id}] ❌ Ошибка автопилота: {err_txt}", "error", "autopilot")
                    return

                transient_retries = 0
                it["views_total"] = max(int(it["views_total"]), int(res.initial_views or 0))
                it["last_round_finished_ts"] = time.time()
                it["patience_extensions"] = 0
                it["next_round_topup"] = False

                # ── PHASE 2: DEDICATED POST-UPLOAD 2048 WAVE SETTLE ──
                if cfg.auto_appeal_2048:
                    it["phase"] = "appealing"
                    it["detail"] = f"Дожим волн 2048 (раунд #{next_round})..."
                    it["updated_at"] = _now_hms()
                    self._save_persisted_state()
                    self._emit("", "info", "autopilot")

                    async with appeal_sem:
                        if self._stop_event.is_set():
                            return
                        try:
                            acc = self._get_store().load_account(account_id)
                            appeal_fn = self._appeal_fn or self._default_appeal_wave
                            ap_sum = await appeal_fn(acc, cfg, self._stop_event.is_set)
                            sent_now = ap_sum.notice_appeals_verified + ap_sum.post_appeals_verified
                            it["appeals_sent"] = int(it["appeals_sent"]) + sent_now
                            if ap_sum.audit is not None:
                                it["health"] = ap_sum.audit.account_health
                                fkey, _ = classify_appeal_folder(ap_sum.audit)
                                it["folder_key"] = fkey
                                sort_account_into_folder(Path(self._get_cfg().state_dir), fkey, acc)
                                if self._on_audit:
                                    ad = asdict(ap_sum.audit)
                                    ad["audited_at"] = _now_hms()
                                    self._on_audit(account_id, ad)
                            if sent_now > 0:
                                self._emit(
                                    f"[{account_id}] ⚡ Дожим 2048 снял {sent_now} ограничений",
                                    "success",
                                    "audit_updated",
                                )
                        except asyncio.CancelledError:
                            raise
                        except Exception as ap_exc:
                            self._emit(f"[{account_id}] ⚠️ Пост-залив 2048: {ap_exc}", "warn", "autopilot")

                if self._stop_event.is_set():
                    return

                # ── PHASE 3: ZERO-COST LIVE VIEW & MODERATION MONITORING ──
                it["phase"] = "monitoring"
                round_start_mon_ts = time.time()
                wait_limit_min = float(cfg.reupload_wait_minutes)
                it["detail"] = f"Мониторинг до {cfg.target_views} · 0/{int(wait_limit_min)}м"
                it["updated_at"] = _now_hms()
                self._save_persisted_state()
                self._emit("", "info", "autopilot")

                while not self._stop_event.is_set():
                    if await self._sleep_interruptible(float(cfg.monitor_interval_sec)):
                        return

                    try:
                        acc = self._get_store().load_account(account_id)
                        mon_fn = self._monitor_fn or self._default_monitor_tick
                        snap = await mon_fn(acc, cfg)
                        v_tot = int(snap.get("views_total") or 0)
                        v_best = int(snap.get("best_post_views") or 0)
                        p_live = int(snap.get("posts_live") or 0)
                        ap_add = int(snap.get("appeals_sent") or 0)

                        # Keep monotonic high-watermark for total_views within same round unless posts were cleaned
                        it["views_total"] = max(int(it["views_total"]), v_tot) if p_live > 0 else v_tot
                        it["best_post_views"] = max(int(it["best_post_views"]), v_best)
                        it["posts_live"] = p_live
                        it["appeals_sent"] = int(it["appeals_sent"]) + ap_add
                        it["health"] = snap.get("health") or it["health"]
                        if snap.get("folder_key"):
                            it["folder_key"] = snap["folder_key"]
                        if snap.get("username"):
                            it["username"] = snap["username"]
                        it["copy_line"] = format_account_copy_line(acc)

                        hist = it["views_history"]
                        hist.append([round(time.time(), 1), it["views_total"]])
                        if len(hist) > 40:
                            del hist[:-40]
                        it["views_velocity"] = self._compute_velocity_10m(hist)

                        if ap_add > 0:
                            self._emit(
                                f"[{account_id}] 🛡️ Мониторинг поймал и обжаловал +{ap_add} флагов (2048/Studio)",
                                "success",
                                "audit_updated",
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as m_exc:
                        it["detail"] = f"Мониторинг (сеть: {str(m_exc)[:40]})"
                        it["updated_at"] = _now_hms()
                        self._emit("", "info", "autopilot")
                        continue

                    # Check target reached!
                    metric = it["best_post_views"] if cfg.target_mode == "max_post" else it["views_total"]
                    if metric >= cfg.target_views:
                        it["phase"] = "goal_reached"
                        it["detail"] = f"🎯 Цель достигнута: {metric}/{cfg.target_views}"
                        it["updated_at"] = _now_hms()
                        try:
                            acc = self._get_store().load_account(account_id)
                            sort_account_into_folder(Path(self._get_cfg().state_dir), "clean", acc)
                            it["folder_key"] = "clean"
                        except Exception:
                            pass
                        self._save_persisted_state()
                        self._emit(
                            f"[{account_id}] 🎯 ЦЕЛЬ {cfg.target_views}+ ДОСТИГНУТА! Просмотров: {it['views_total']} "
                            f"(лучший пост: {it['best_post_views']}, раунд #{next_round})",
                            "success",
                            "audit_updated",
                        )
                        return

                    elapsed_min = (time.time() - round_start_mon_ts) / 60.0
                    vel = int(it["views_velocity"] or 0)
                    it["detail"] = (
                        f"Мониторинг {int(elapsed_min)}/{int(wait_limit_min)}м"
                        + (f" · +{vel}/10м" if vel > 0 else "")
                    )
                    it["updated_at"] = _now_hms()
                    self._save_persisted_state()
                    self._emit("", "info", "autopilot")

                    if elapsed_min >= wait_limit_min:
                        # Smart patience extension if actively climbing toward 600!
                        if (
                            vel >= cfg.growth_extend_views
                            and it["views_total"] >= max(150, int(cfg.target_views * 0.3))
                            and wait_limit_min + 10.0 <= float(cfg.max_patience_minutes)
                        ):
                            wait_limit_min += 10.0
                            it["patience_extensions"] = int(it["patience_extensions"]) + 1
                            it["detail"] = f"📈 Растёт (+{vel}/10м), ждём до {int(wait_limit_min)}м"
                            self._emit(
                                f"[{account_id}] 📈 Активно набирает (+{vel}/10м, сейчас {it['views_total']}/{cfg.target_views}) — продлеваю ожидание +10м",
                                "info",
                                "autopilot",
                            )
                            continue

                        # Decide next round strategy: Smart Top-Up vs Clean Re-Pour
                        if next_round < cfg.max_rounds:
                            if it["views_total"] >= cfg.protect_views_threshold:
                                it["next_round_topup"] = True
                                self._emit(
                                    f"[{account_id}] ➕ Просмотры {it['views_total']}/{cfg.target_views} живые — "
                                    f"доливаю +{cfg.topup_copies} свежих постов без удаления старых!",
                                    "info",
                                    "autopilot",
                                )
                            else:
                                low_v = int(it["views_total"] or 0)
                                it["next_round_topup"] = False
                                it["views_total"] = 0
                                it["best_post_views"] = 0
                                it["views_history"] = []
                                self._emit(
                                    f"[{account_id}] 🔄 Низкий охват ({low_v}/{cfg.target_views}) — "
                                    f"чищу посты, меняю уник и перезаливаю раунд #{next_round + 1}/{cfg.max_rounds}",
                                    "warn",
                                    "autopilot",
                                )
                        break  # Exit monitoring loop -> next round or capped

        except asyncio.CancelledError:
            if it["phase"] not in ("goal_reached", "capped", "error"):
                it["phase"] = "stopped"
                it["detail"] = "Остановлено"
                it["updated_at"] = _now_hms()
        except Exception as exc:
            it["phase"] = "error"
            it["error"] = str(exc)
            it["detail"] = f"Ошибка: {str(exc)[:70]}"
            it["updated_at"] = _now_hms()
            self._emit(f"[{account_id}] ❌ Критическая ошибка автопилота: {exc}", "error", "autopilot")
        finally:
            self._busy_upload.discard(account_id)
            self._managed.discard(account_id)
            self._save_persisted_state()

    def build_copy_payload(self, *, scope: str = "reached", fmt: str = "lines") -> dict[str, Any]:
        items = list(self.items.values())
        if scope == "reached":
            filtered = [x for x in items if x["phase"] == "goal_reached"]
        elif scope == "active":
            filtered = [x for x in items if x["phase"] in ("queued", "uploading", "appealing", "monitoring")]
        else:
            filtered = items

        lines: list[str] = []
        seen_urls: set[str] = set()
        for x in filtered:
            handle = str(x.get("username") or x["account_id"]).lstrip("@")
            if fmt == "usernames":
                lines.append(f"@{handle}")
            elif fmt == "urls":
                for u in x.get("post_urls") or []:
                    if u not in seen_urls:
                        seen_urls.add(u)
                        lines.append(u)
            elif fmt == "summary":
                lines.append(
                    f"@{handle} | views={x.get('views_total', 0)}/{x.get('target_views', 600)} | "
                    f"posts={x.get('posts_live', x.get('posts_published', 0))} | round={x.get('round_num', 0)} | "
                    f"{x.get('copy_line') or handle}"
                )
            else:
                lines.append(str(x.get("copy_line") or handle))

        return {
            "ok": True,
            "scope": scope,
            "fmt": fmt,
            "count": len(lines),
            "accounts_count": len(filtered),
            "text": "\n".join(lines),
        }

    def snapshot(self) -> dict[str, Any]:
        items = list(self.items.values())
        phase_order = {
            "uploading": 0,
            "appealing": 1,
            "monitoring": 2,
            "queued": 3,
            "goal_reached": 4,
            "capped": 5,
            "error": 6,
            "stopped": 7,
        }
        items.sort(key=lambda x: (phase_order.get(x["phase"], 9), -int(x.get("views_total") or 0), x["account_id"]))

        counts = {
            "total": len(items),
            "queued": sum(1 for x in items if x["phase"] == "queued"),
            "uploading": sum(1 for x in items if x["phase"] == "uploading"),
            "appealing": sum(1 for x in items if x["phase"] == "appealing"),
            "monitoring": sum(1 for x in items if x["phase"] == "monitoring"),
            "goal_reached": sum(1 for x in items if x["phase"] == "goal_reached"),
            "capped": sum(1 for x in items if x["phase"] == "capped"),
            "error": sum(1 for x in items if x["phase"] == "error"),
            "stopped": sum(1 for x in items if x["phase"] == "stopped"),
            "total_views": sum(int(x.get("views_total") or 0) for x in items),
            "total_posts": sum(int(x.get("posts_published") or 0) for x in items),
            "total_appeals": sum(int(x.get("appeals_sent") or 0) for x in items),
        }
        active_cnt = counts["queued"] + counts["uploading"] + counts["appealing"] + counts["monitoring"]
        finished_cnt = counts["goal_reached"] + counts["capped"] + counts["error"]
        avg_views = int(round(counts["total_views"] / len(items))) if items else 0
        success_rate = int(round((counts["goal_reached"] / finished_cnt) * 100)) if finished_cnt else 0

        return {
            "ok": True,
            "run_id": self.run_id,
            "running": self.running,
            "stopping": self.stopping,
            "started_at": self.started_at,
            "elapsed_sec": int(time.time() - self.started_ts) if (self.running and self.started_ts > 0) else 0,
            "active_count": active_cnt,
            "avg_views": avg_views,
            "success_rate_pct": success_rate,
            "config": self.config.to_dict(),
            "counts": counts,
            "items": items,
        }

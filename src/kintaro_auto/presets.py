"""
Campaign Presets & Server Media Library Manager (merged from swegby/kintaro_auth).
Stores presets in `state/campaign_presets.json` and reusable creatives in `state/media/`.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kintaro_auto.uniquifier import PHOTO_UNIQUIFIER_PRESETS

DEFAULT_CAMPAIGN_PRESETS: list[dict[str, Any]] = [
    {
        "id": "us_6posts_minimal_snow",
        "name": "US Anti-Shadow — 6 постов (minimal_snow + Рестарт после 3 + 3-этапный 2048 Auto-Appeal)",
        "threads": 5,
        "copies_count": 6,
        "delay_min_sec": 14.0,
        "delay_max_sec": 20.0,
        "restart_every_n": 3,
        "region": "US",
        "phone_region": "US",
        "app_locale": "en_US",
        "delete_old_posts": True,
        "randomize_tags": True,
        "auto_music_query": "Fortnite",
        "photo_mode": "minimal_snow",
        "uniquifier": dict(PHOTO_UNIQUIFIER_PRESETS["minimal_snow"]),
        "auto_appeal_2048": True,
        "appeal_pre_upload": True,
        "appeal_inter_post": True,
        "appeal_post_upload": True,
        "appeal_post_wait_sec": 8.0,
        "appeal_quiet_window_sec": 60.0,
        "description": "#piratetoken #piratetokenshop #blackknight #pirateshop #fyp",
    },
    {
        "id": "it_6posts_boosted",
        "name": "IT — 6 постов (Усиленный снег boosted + Fortnite + Рестарт после 3)",
        "threads": 3,
        "copies_count": 6,
        "delay_min_sec": 10.0,
        "delay_max_sec": 15.0,
        "restart_every_n": 3,
        "region": "IT",
        "phone_region": "IT",
        "app_locale": "it_IT",
        "delete_old_posts": True,
        "randomize_tags": True,
        "auto_music_query": "Fortnite",
        "photo_mode": "boosted",
        "uniquifier": dict(PHOTO_UNIQUIFIER_PRESETS["boosted"]),
        "auto_appeal_2048": True,
        "appeal_pre_upload": True,
        "appeal_inter_post": True,
        "appeal_post_upload": True,
        "appeal_post_wait_sec": 8.0,
        "appeal_quiet_window_sec": 60.0,
        "description": "#piratetoken #piratetokenshop #blackknight #pirateshop #fyp",
    },
    {
        "id": "ultra_hard_stealth",
        "name": "Ultra-Hard Anti-Duplicate — 3D перспектива + плавающий фон + 3-этапный 2048",
        "threads": 5,
        "copies_count": 6,
        "delay_min_sec": 14.0,
        "delay_max_sec": 20.0,
        "restart_every_n": 3,
        "region": "US",
        "phone_region": "US",
        "app_locale": "en_US",
        "delete_old_posts": True,
        "randomize_tags": True,
        "auto_music_query": "Fortnite",
        "photo_mode": "ultra_hard",
        "uniquifier": dict(PHOTO_UNIQUIFIER_PRESETS["ultra_hard"]),
        "auto_appeal_2048": True,
        "appeal_pre_upload": True,
        "appeal_inter_post": True,
        "appeal_post_upload": True,
        "appeal_post_wait_sec": 8.0,
        "appeal_quiet_window_sec": 60.0,
        "description": "#fyp #gaming",
    },
]


def load_campaign_presets(state_dir: Path) -> list[dict[str, Any]]:
    pfile = Path(state_dir) / "campaign_presets.json"
    if pfile.exists():
        try:
            items = json.loads(pfile.read_text(encoding="utf-8"))
            if isinstance(items, list) and items:
                return items
        except Exception:
            pass
    pfile.parent.mkdir(parents=True, exist_ok=True)
    pfile.write_text(json.dumps(DEFAULT_CAMPAIGN_PRESETS, indent=2, ensure_ascii=False), encoding="utf-8")
    return list(DEFAULT_CAMPAIGN_PRESETS)


def save_campaign_preset(state_dir: Path, preset: dict[str, Any]) -> list[dict[str, Any]]:
    items = load_campaign_presets(state_dir)
    pid = re.sub(r"[^a-zA-Z0-9_-]", "_", str(preset.get("id") or "")).strip("_") or f"preset_{int(time.time())}"
    preset["id"] = pid
    preset["updated_at"] = datetime.now(timezone.utc).isoformat()
    for idx, existing in enumerate(items):
        if existing.get("id") == pid:
            items[idx] = preset
            break
    else:
        items.insert(0, preset)
    pfile = Path(state_dir) / "campaign_presets.json"
    pfile.write_text(json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    return items


def list_media_library(state_dir: Path) -> list[dict[str, Any]]:
    mdir = Path(state_dir) / "media"
    mdir.mkdir(parents=True, exist_ok=True)
    files: list[dict[str, Any]] = []
    for fp in sorted(mdir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        ext = fp.suffix.lower()
        if fp.is_file() and ext in (".jpg", ".jpeg", ".png", ".webp", ".mp4"):
            st = fp.stat()
            files.append(
                {
                    "name": fp.name,
                    "path": str(fp),
                    "kind": "video" if ext == ".mp4" else "photo",
                    "size_kb": round(st.st_size / 1024.0, 1),
                    "mtime": int(st.st_mtime),
                }
            )
    return files

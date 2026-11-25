"""
Competitor Spy, Watermark-Free Creative Cloner, Top-Comment Pinning/Boost & Live TOTP 2FA Generator
(merged cleanly from swegby/kintaro_auth).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import io
import random
import re
import struct
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from kintaro_auto.captions import resolve_spintax
from kintaro_auto.client import KintaroClient
from kintaro_auto.models import AccountRecord
from kintaro_auto.storage import StateStore


def extract_aweme_id_from_input(raw: str) -> str:
    """Extracts numeric TikTok `aweme_id` from a full URL (`/video/123`, `/photo/123`) or raw ID."""
    txt = (raw or "").strip()
    if not txt:
        return ""
    if txt.isdigit() and len(txt) >= 12:
        return txt
    m = re.search(r"/(?:video|photo)/(\d{12,24})", txt)
    if m:
        return m.group(1)
    m2 = re.search(r"\b(\d{15,24})\b", txt)
    return m2.group(1) if m2 else ""


def generate_live_totp(secret_b32: str, *, timestamp: float | None = None, step: int = 30) -> dict[str, Any]:
    """Computes RFC 6238 6-digit TOTP code + remaining seconds for quick manual phone/web login."""
    clean = re.sub(r"[\s\-]+", "", (secret_b32 or "")).upper()
    if not clean:
        raise ValueError("Пустой TOTP секрет.")
    pad_len = (-len(clean)) % 8
    key = base64.b32decode(clean + ("=" * pad_len), casefold=True)
    now = time.time() if timestamp is None else float(timestamp)
    counter = int(now // step)
    remaining = step - int(now % step)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return {"code": f"{code_int:06d}", "remaining_sec": remaining, "period": step}


def _extract_media_urls_from_aweme(it: dict[str, Any]) -> tuple[list[str], str]:
    img_post = it.get("image_post_info") or {}
    slide_urls: list[str] = []
    for img in img_post.get("images") or []:
        if not isinstance(img, dict):
            continue
        u_list = (img.get("display_image") or {}).get("url_list") or []
        if u_list:
            slide_urls.append(str(u_list[0]))
    vid = it.get("video") or {}
    play_list = (vid.get("play_addr") or {}).get("url_list") or (vid.get("download_addr") or {}).get("url_list") or []
    video_url = str(play_list[0]) if play_list else ""
    return slide_urls, video_url


async def spy_competitor_profile(
    client: KintaroClient,
    account: AccountRecord,
    target_username: str,
    *,
    count: int = 18,
    region: str = "",
) -> dict[str, Any]:
    """
    Fetches a competitor's profile & recent posts (or a single TikTok post URL) via Kintaro API,
    computing total/avg/max views, top hashtags, top viral Music IDs, and direct post/slide URLs.
    """
    raw = (target_username or "").strip()
    single_aid = extract_aweme_id_from_input(raw)
    if single_aid and ("tiktok.com" in raw.lower() or raw.isdigit()):
        p_raw = await client.get_post(account, single_aid)
        details = (
            p_raw.get("aweme_details")
            or ([p_raw.get("aweme_detail")] if isinstance(p_raw.get("aweme_detail"), dict) else [])
            or ([p_raw.get("aweme")] if isinstance(p_raw.get("aweme"), dict) else [])
        )
        aweme_list = [d for d in details if isinstance(d, dict)]
        uname = ((aweme_list[0].get("author") or {}).get("unique_id") if aweme_list else "") or "post"
        user_obj = (aweme_list[0].get("author") or {}) if aweme_list else {}
    else:
        m_u = re.search(r"tiktok\.com/@([A-Za-z0-9_.]+)", raw)
        uname = (m_u.group(1) if m_u else raw).lstrip("@").strip()
        if not uname:
            raise ValueError("Не указан username конкурента или ссылка на пост.")
        prof_raw = await client.get_user_profile(account, uname)
        posts_raw = await client.get_user_posts(account, uname, count=count, region_override=region or None)
        user_obj = (
            prof_raw.get("user")
            or (prof_raw.get("data") or {}).get("user")
            or prof_raw
            if isinstance(prof_raw, dict)
            else {}
        )
        aweme_list = (
            posts_raw.get("aweme_list")
            or (posts_raw.get("data") or {}).get("aweme_list")
            or []
            if isinstance(posts_raw, dict)
            else []
        )

    posts_out: list[dict[str, Any]] = []
    tag_counter: Counter[str] = Counter()
    music_counter: Counter[str] = Counter()
    music_labels: dict[str, str] = {}

    for it in aweme_list:
        if not isinstance(it, dict):
            continue
        aid = str(it.get("aweme_id") or "")
        desc = str(it.get("desc") or "")
        st = it.get("statistics") or {}
        views = int(st.get("play_count") or 0)
        likes = int(st.get("digg_count") or 0)
        comments = int(st.get("comment_count") or 0)
        shares = int(st.get("share_count") or 0)

        for tag in re.findall(r"#[\w\d_]+", desc.lower()):
            tag_counter[tag] += 1

        mus = it.get("music") or {}
        mid = str(mus.get("id_str") or mus.get("mid") or mus.get("id") or "")
        if mid:
            music_counter[mid] += 1
            music_labels[mid] = f"{mus.get('title') or 'Sound'} — {mus.get('author') or ''}".strip(" —")

        slide_urls, video_url = _extract_media_urls_from_aweme(it)
        kind = "photo" if slide_urls else "video"
        posts_out.append(
            {
                "aweme_id": aid,
                "kind": kind,
                "desc": desc,
                "region": str(it.get("region") or ""),
                "views": views,
                "likes": likes,
                "comments": comments,
                "shares": shares,
                "music_id": mid,
                "music_label": music_labels.get(mid, ""),
                "slides_count": len(slide_urls),
                "slide_urls": slide_urls,
                "video_url": video_url,
                "url": f"https://www.tiktok.com/@{uname}/{'photo' if kind == 'photo' else 'video'}/{aid}" if aid else "",
            }
        )

    views_list = [p["views"] for p in posts_out]
    total_views = sum(views_list)
    avg_views = int(round(total_views / len(views_list))) if views_list else 0
    max_views = max(views_list) if views_list else 0

    return {
        "username": uname,
        "nickname": str(user_obj.get("nickname") or uname),
        "followers": int(user_obj.get("follower_count") or 0),
        "likes_total": int(user_obj.get("total_favorited") or 0),
        "bio": str(user_obj.get("signature") or ""),
        "posts_scanned": len(posts_out),
        "total_views": total_views,
        "avg_views": avg_views,
        "max_views": max_views,
        "top_hashtags": [{"tag": t, "count": c} for t, c in tag_counter.most_common(12)],
        "top_music": [
            {"music_id": mid, "label": music_labels.get(mid, mid), "count": cnt}
            for mid, cnt in music_counter.most_common(8)
        ],
        "posts": posts_out,
    }


async def download_creative_to_media(
    client: KintaroClient,
    account: AccountRecord,
    aweme_id_or_url: str,
    state_dir: Path,
) -> dict[str, Any]:
    """Downloads watermark-free carousel slides (converting WebP -> RGB JPEG) or MP4 straight into `state/media/`."""
    aid = extract_aweme_id_from_input(aweme_id_or_url)
    if not aid:
        raise ValueError("Не удалось распознать ID поста TikTok.")

    p_raw = await client.get_post(account, aid)
    details = (
        p_raw.get("aweme_details")
        or ([p_raw.get("aweme_detail")] if isinstance(p_raw.get("aweme_detail"), dict) else [])
        or ([p_raw.get("aweme")] if isinstance(p_raw.get("aweme"), dict) else [])
    )
    if not details or not isinstance(details[0], dict):
        raise RuntimeError(f"Пост {aid} не найден.")

    item = details[0]
    slide_urls, video_url = _extract_media_urls_from_aweme(item)
    mdir = Path(state_dir) / "media"
    mdir.mkdir(parents=True, exist_ok=True)
    saved_files: list[str] = []

    dl_headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15"}
    async with httpx.AsyncClient(timeout=35.0, follow_redirects=True, headers=dl_headers) as dl:
        if slide_urls:
            for idx, surl in enumerate(slide_urls[:10], start=1):
                r = await dl.get(surl)
                r.raise_for_status()
                fname = f"spy_{aid}_{idx:02d}.jpg"
                out_fp = mdir / fname

                def _save_jpg(raw_b: bytes = r.content, dest: Path = out_fp) -> None:
                    with Image.open(io.BytesIO(raw_b)) as im:
                        im.convert("RGB").save(dest, format="JPEG", quality=95, subsampling=0)

                await asyncio.to_thread(_save_jpg)
                saved_files.append(fname)
        elif video_url:
            r = await dl.get(video_url)
            r.raise_for_status()
            fname = f"spy_{aid}.mp4"
            (mdir / fname).write_bytes(r.content)
            saved_files.append(fname)
        else:
            raise RuntimeError("В посте не найдены прямые ссылки на слайды или видео.")

    mus = item.get("music") or {}
    return {
        "aweme_id": aid,
        "kind": "photo" if slide_urls else "video",
        "saved_files": saved_files,
        "desc": str(item.get("desc") or ""),
        "music_id": str(mus.get("id_str") or mus.get("mid") or mus.get("id") or ""),
        "music_title": str(mus.get("title") or ""),
    }


async def fetch_post_comments_structured(
    client: KintaroClient,
    account: AccountRecord,
    aweme_id_or_url: str,
    *,
    count: int = 30,
) -> dict[str, Any]:
    aid = extract_aweme_id_from_input(aweme_id_or_url)
    if not aid:
        raise ValueError("Укажи корректную ссылку на пост или AWEME ID.")
    c_body = await client.get_post_comments(account, aid, count=count)
    raw_comments = (c_body.get("comments") if isinstance(c_body, dict) else None) or []
    parsed: list[dict[str, Any]] = []
    for cm in raw_comments:
        if not isinstance(cm, dict):
            continue
        u = cm.get("user") or {}
        ct = int(cm.get("create_time") or 0)
        parsed.append(
            {
                "cid": str(cm.get("cid") or ""),
                "text": str(cm.get("text") or ""),
                "username": str(u.get("unique_id") or u.get("nickname") or ""),
                "likes": int(cm.get("digg_count") or 0),
                "replies": int(cm.get("reply_comment_total") or 0),
                "created_at": datetime.fromtimestamp(ct, tz=timezone.utc).strftime("%d.%m %H:%M") if ct else "",
            }
        )
    return {"aweme_id": aid, "total": int((c_body or {}).get("total") or len(parsed)), "comments": parsed}


def _extract_cid(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    for container in (body, body.get("comment"), body.get("data"), (body.get("data") or {}).get("comment") if isinstance(body.get("data"), dict) else None):
        if isinstance(container, dict) and container.get("cid"):
            return str(container["cid"])
    return ""


async def run_top_comment_boost(
    client: KintaroClient,
    store: StateStore,
    *,
    target_post: str,
    seed_account_id: str = "",
    lead_comment_text: str = "",
    existing_cid: str = "",
    booster_account_ids: list[str] | None = None,
    reply_templates: list[str] | None = None,
    reply_prob_pct: int = 30,
    threads: int = 4,
) -> dict[str, Any]:
    """
    Publishes a lead comment (with spintax & @mentions) from `seed_account_id` (or targets `existing_cid`),
    then uses `booster_account_ids` concurrently to LIKE the comment and optionally reply to push it #1 TOP!
    """
    aid = extract_aweme_id_from_input(target_post)
    if not aid:
        raise ValueError("Не указан целевой пост (ссылка или AWEME ID).")

    cid = existing_cid.strip()
    if not cid:
        if not seed_account_id or not lead_comment_text.strip():
            raise ValueError("Укажи аккаунт автора и текст комментария (или существующий CID).")
        seed_acc = store.load_account(seed_account_id)
        final_text = resolve_spintax(lead_comment_text.strip())
        mentions = re.findall(r"@([A-Za-z0-9_.]+)", final_text)
        try:
            await client.view_post(seed_acc, aid, random.randint(4500, 8500))
        except Exception:
            pass
        pub_resp = await client.publish_comment(seed_acc, aid, final_text, mentions=mentions or None)
        store.save_account(seed_acc)
        cid = _extract_cid(pub_resp)
        if not cid:
            raise RuntimeError(f"Комментарий отправлен, но `cid` не найден в ответе: {list(pub_resp.keys())}")

    boosters = [
        store.load_account(bid)
        for bid in (booster_account_ids or [])
        if bid and bid != seed_account_id
    ]
    if not boosters:
        boosters = [a for a in store.list_accounts() if a.authenticated and a.account_id != seed_account_id]

    sem = asyncio.Semaphore(max(1, min(15, threads)))
    likes_ok = 0
    replies_ok = 0
    errors: list[str] = []
    replies_clean = [r.strip() for r in (reply_templates or []) if r.strip()]

    async def _boost_one(b_acc: AccountRecord) -> None:
        nonlocal likes_ok, replies_ok
        async with sem:
            try:
                await client.like_comment(b_acc, aid, cid)
                likes_ok += 1
                if replies_clean and random.randint(1, 100) <= reply_prob_pct:
                    rep_txt = resolve_spintax(random.choice(replies_clean))
                    await client.publish_comment(b_acc, aid, rep_txt, reply_id=cid)
                    replies_ok += 1
                store.save_account(b_acc)
            except Exception as exc:
                errors.append(f"{b_acc.account_id}: {exc}")

    await asyncio.gather(*(_boost_one(b) for b in boosters))
    return {
        "aweme_id": aid,
        "cid": cid,
        "likes_ok": likes_ok,
        "replies_ok": replies_ok,
        "boosters_total": len(boosters),
        "errors": errors,
    }

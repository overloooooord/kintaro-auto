"""
Competitor Spy, Live TOTP 2FA Generator & Comment Marketing Helpers (merged from swegby/kintaro_auth).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import struct
import time
from collections import Counter
from typing import Any

from kintaro_auto.client import KintaroClient
from kintaro_auto.models import AccountRecord


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


async def spy_competitor_profile(
    client: KintaroClient,
    account: AccountRecord,
    target_username: str,
    *,
    count: int = 18,
    region: str = "",
) -> dict[str, Any]:
    """
    Fetches a competitor's profile and recent posts via Kintaro API (`/v1/user/get` + `/v1/user/posts`),
    computing total/avg/max views, top hashtags, top viral Music IDs, and direct post/slide URLs.
    """
    uname = target_username.lstrip("@").strip()
    if not uname:
        raise ValueError("Не указан username конкурента.")

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

        img_post = it.get("image_post_info") or {}
        slide_urls: list[str] = []
        for img in img_post.get("images") or []:
            u_list = ((img.get("display_image") or {}).get("url_list") or []) if isinstance(img, dict) else []
            if u_list:
                slide_urls.append(str(u_list[0]))

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

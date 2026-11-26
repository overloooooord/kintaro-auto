"""
Pro TikTok Shadowban & Moderation Checker (`shadow_checker.py`).

Checks accounts & posts using REAL internal TikTok moderation & distribution fields
returned by signed Kintaro API v1 + optional TikTok Studio & Account Updates 2048 audit:
  - Per-post internal flags:
      * `is_nff_or_nr` (Not For Feed / Not Recommended -> FYP shadowban!)
      * `risk_infos.risk_sink` / `risk_infos.warn` / `risk_infos.type`
      * `status.is_prohibited`, `status.in_reviewing`, `status.reviewed`,
        `status.self_see`, `status.private_status`, `status.allow_share`,
        `status.review_result.review_status`, `status.violation_display_status`
      * Public vs Owner feed diff (catches posts silently hidden from `/v1/user/posts`)
      * Zero-views / age velocity check (ignores fresh posts < `zero_view_threshold_minutes`)
  - Account-level checks (optional `deep_audit=True` for authenticated fleet accounts):
      * Account Updates 2048 (`eligible` / `appealing` / `action_required`)
      * TikTok Studio FYP post penalties
  - Works in two modes:
      1. Fast public batch checker for ANY `@username` list in `N` parallel threads
      2. Deep checker for your own fleet (Public + Owner + Studio + Account Updates 2048)
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from kintaro_auto.appeals import audit_account_restrictions
from kintaro_auto.client import KintaroClient
from kintaro_auto.models import AccountRecord
from kintaro_auto.storage import StateStore, format_account_copy_line

COUNTRY_NAMES: dict[str, str] = {
    "US": "United States",
    "GB": "United Kingdom",
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "CA": "Canada",
    "AU": "Australia",
    "BR": "Brazil",
    "PT": "Portugal",
    "PL": "Poland",
    "NL": "Netherlands",
    "SE": "Sweden",
    "NO": "Norway",
    "FI": "Finland",
    "CZ": "Czechia",
    "RO": "Romania",
    "UA": "Ukraine",
    "KZ": "Kazakhstan",
    "RU": "Russia",
    "TR": "Turkey",
    "VN": "Vietnam",
    "SG": "Singapore",
    "ID": "Indonesia",
    "TH": "Thailand",
    "MY": "Malaysia",
    "PH": "Philippines",
    "JP": "Japan",
    "KR": "South Korea",
    "MX": "Mexico",
    "AR": "Argentina",
    "CO": "Colombia",
    "CL": "Chile",
}

_HASHTAG_RE = re.compile(r"#[^\s#]+")


def country_flag_and_name(cc: str) -> tuple[str, str]:
    code = (cc or "").strip().upper()
    if len(code) == 2 and code.isalpha():
        flag = "".join(chr(0x1F1E6 + ord(ch) - ord("A")) for ch in code)
        return flag, COUNTRY_NAMES.get(code, code)
    return "🌐", code or "Unknown"


def _fmt_epoch_local(ts: int | float | None) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "—"


def _extract_hashtags(item: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    seen: set[str] = set()
    desc = str(item.get("desc") or "")
    for m in _HASHTAG_RE.findall(desc):
        low = m.lower()
        if low not in seen:
            seen.add(low)
            tags.append(m)
    for te in item.get("text_extra") or []:
        if isinstance(te, dict):
            hn = str(te.get("hashtag_name") or "").strip()
            if hn:
                ht = f"#{hn}"
                if ht.lower() not in seen:
                    seen.add(ht.lower())
                    tags.append(ht)
    return tags


def inspect_aweme_shadow_signals(
    item: dict[str, Any],
    *,
    username: str = "",
    is_public_visible: bool = True,
    studio_flagged: bool = False,
    studio_penalty_info: dict[str, Any] | None = None,
    zero_view_threshold_minutes: float = 20.0,
    now_ts: float | None = None,
) -> dict[str, Any]:
    """Extracts complete TikTok moderation & shadowban signals from a raw `aweme` object."""
    now = now_ts if now_ts is not None else time.time()
    aweme_id = str(item.get("aweme_id") or item.get("item_id") or "").strip()
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    handle = (username or str(author.get("unique_id") or "")).strip().lstrip("@")

    stats = item.get("statistics") if isinstance(item.get("statistics"), dict) else {}
    views = int(stats.get("play_count") or 0)
    likes = int(stats.get("digg_count") or 0)
    comments = int(stats.get("comment_count") or 0)
    shares = int(stats.get("share_count") or 0)
    saves = int(stats.get("collect_count") or 0)

    status = item.get("status") if isinstance(item.get("status"), dict) else {}
    risk = item.get("risk_infos") if isinstance(item.get("risk_infos"), dict) else {}
    vctrl = item.get("video_control") if isinstance(item.get("video_control"), dict) else {}
    review_res = status.get("review_result") if isinstance(status.get("review_result"), dict) else {}

    is_delete = bool(status.get("is_delete", False))
    is_prohibited = bool(status.get("is_prohibited", False))
    in_reviewing = bool(status.get("in_reviewing", False))
    reviewed = status.get("reviewed", 1)
    self_see = bool(status.get("self_see", False))
    private_status = int(status.get("private_status") or 0)
    allow_share = status.get("allow_share", True) is not False
    violation_display = int(status.get("violation_display_status") or 0)
    review_status = int(review_res.get("review_status") or 0)

    is_nff_or_nr = bool(item.get("is_nff_or_nr", False))
    risk_sink = bool(risk.get("risk_sink", False))
    risk_warn = bool(risk.get("warn", False))
    risk_type = int(risk.get("type") or 0)
    distribute_type = item.get("distribute_type")

    create_time = int(item.get("create_time") or 0)
    age_minutes = max(0.0, (now - create_time) / 60.0) if create_time > 0 else 0.0
    region = str(item.get("region") or "").strip().upper()
    flag, region_name = country_flag_and_name(region)

    is_photo = item.get("aweme_type") == 150 or bool(item.get("image_post_info"))
    seg = "photo" if is_photo else "video"
    url = f"https://www.tiktok.com/@{handle}/{seg}/{aweme_id}" if (handle and aweme_id) else ""
    hashtags = _extract_hashtags(item)
    desc = str(item.get("desc") or "").strip()

    # 1. Moderation status
    if is_delete:
        mod_code, mod_label = "deleted", "Удалён"
    elif is_prohibited or violation_display != 0 or review_status != 0:
        mod_code, mod_label = "prohibited", "Нарушение / Запрещён"
    elif in_reviewing or reviewed == 0:
        mod_code, mod_label = "reviewing", "На проверке"
    elif self_see or private_status != 0:
        mod_code, mod_label = "private", "Только автор (Private)"
    else:
        mod_code, mod_label = "passed", "Пройдена"

    # 2. Honest shadowban signals (ONLY real public/FYP shadow flags or 0 views — NOT Studio UNORIGINAL_NR!)
    hard_public_reasons: list[str] = []
    soft_reasons: list[str] = []

    if is_nff_or_nr:
        hard_public_reasons.append("is_nff_or_nr=true (исключён из FYP)")
    if risk_sink:
        hard_public_reasons.append("risk_sink=true (понижен в выдаче)")
    if is_prohibited:
        hard_public_reasons.append("is_prohibited=true (запрещён)")
    if self_see:
        hard_public_reasons.append("self_see=true (виден только автору)")
    if not is_public_visible and private_status == 0 and not is_delete:
        hard_public_reasons.append("Скрыт из публички (3020004 unavailable)")
    if create_time > 0 and age_minutes >= zero_view_threshold_minutes and views == 0:
        hard_public_reasons.append(f"0 просмотров за {int(age_minutes)} мин")

    if in_reviewing:
        soft_reasons.append("in_reviewing=true (на проверке)")
    if not allow_share:
        soft_reasons.append("allow_share=false (репост закрыт)")
    if risk_warn or risk_type != 0:
        soft_reasons.append(f"risk_infos(warn={risk_warn}, type={risk_type})")

    # Separate informational TikTok Studio FYP note (does NOT falsely mark a live multi-view post as shadowbanned!)
    studio_can_submit = False
    studio_status_label = ""
    studio_note = ""
    if studio_flagged or studio_penalty_info:
        sp = studio_penalty_info or {}
        labels = [str(x) for x in (sp.get("reason_labels") or []) if x]
        st_lbl = str(sp.get("appeal_status_label") or "")
        studio_status_label = st_lbl
        studio_can_submit = bool(sp.get("can_submit"))
        lbl_txt = ",".join(labels) if labels else "FYP_INELIGIBLE"
        studio_note = f"{lbl_txt}" + (f" ({st_lbl})" if st_lbl else "")

    if hard_public_reasons:
        shadow_code, shadow_label = "yes", "Да"
    elif soft_reasons:
        shadow_code, shadow_label = "suspect", "Подозрение"
    else:
        shadow_code, shadow_label = "no", "Нет"

    return {
        "aweme_id": aweme_id,
        "kind": seg,
        "url": url,
        "desc": desc,
        "hashtags": hashtags,
        "views": views,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "saves": saves,
        "region": region,
        "region_flag": flag,
        "region_name": region_name,
        "create_time": create_time,
        "created_str": _fmt_epoch_local(create_time),
        "age_minutes": round(age_minutes, 1),
        "moderation_code": mod_code,
        "moderation_label": mod_label,
        "shadow_code": shadow_code,
        "shadow_label": shadow_label,
        "reasons": hard_public_reasons + soft_reasons,
        "is_public_visible": is_public_visible,
        "studio_note": studio_note,
        "studio_can_submit": studio_can_submit,
        "studio_status_label": studio_status_label,
        "flags": {
            "is_nff_or_nr": is_nff_or_nr,
            "risk_sink": risk_sink,
            "risk_warn": risk_warn,
            "in_reviewing": in_reviewing,
            "reviewed": reviewed,
            "is_prohibited": is_prohibited,
            "self_see": self_see,
            "private_status": private_status,
            "allow_share": allow_share,
            "distribute_type": distribute_type,
            "allow_download": vctrl.get("allow_download"),
        },
    }


def format_account_telegram_report(report: dict[str, Any]) -> str:
    """Formats an account export / dump in clean KURDY-style Telegram blocks (no fake scores)."""
    uname = report.get("username") or ""
    verdict_lbl = report.get("verdict_label") or ""
    posts = report.get("posts") or []
    if not posts:
        return f"💊 На аккаунте @{uname}\n└ нет публичных постов"

    regs = ",".join(report.get("regions") or []) or "—"
    lines = [
        f"{verdict_lbl} | @{uname} | Постов: {report.get('public_posts_count', len(posts))}/{len(posts)} | 👁 {report.get('total_views', 0)} | {regs}"
    ]
    for idx, p in enumerate(posts, start=1):
        tags_str = " ".join(p.get("hashtags") or [])
        lines.append(
            f"\nВидео {idx}: | @{uname}\n"
            f"👁: {p['views']} ❤️: {p['likes']} 💬: {p['comments']} 📣: {p['shares']} 💾: {p['saves']}\n"
            f"🌟 Тень: {p['shadow_label']}"
            + (f" ({'; '.join(p['reasons'])})" if p.get("reasons") else "")
            + f"\n👮 Модерация: {p['moderation_label']}\n"
            f"{p['region_flag']} {p['region_name']} ~ {p['created_str']}"
            + (f"\n{tags_str}" if tags_str else "")
        )
    return "\n".join(lines)


async def check_account_shadowban(
    client: KintaroClient,
    *,
    probe_account: AccountRecord,
    target_username: str,
    own_account: AccountRecord | None = None,
    deep_audit: bool = False,
    max_posts: int = 12,
    zero_view_threshold_minutes: float = 20.0,
) -> dict[str, Any]:
    """
    Checks a single TikTok account for shadowban / FYP suppression & moderation flags.

    Combines:
      1. Public profile lookup (`/v1/user/get`) & public posts (`/v1/user/posts`) via `probe_account`
      2. If `own_account` is authenticated and `deep_audit=True`:
         - Owner feed (`/v1/account/posts`) diff vs public feed to detect silent hidden posts
         - Account Updates 2048 & TikTok Studio FYP penalties (`audit_account_restrictions`)
    """
    handle = target_username.strip().lstrip("@")
    copy_line = format_account_copy_line(own_account) if own_account is not None else f"@{handle}"
    account_id = own_account.account_id if own_account is not None else handle

    profile_info: dict[str, Any] = {}
    followers = 0
    following = 0
    total_likes = 0
    profile_video_count = 0
    profile_private = False
    profile_error = ""

    # 1) Public profile & public posts
    try:
        prof_resp = await client.get_user_profile(probe_account, handle)
        u = prof_resp.get("user") if isinstance(prof_resp.get("user"), dict) else prof_resp
        if isinstance(u, dict):
            profile_info = u
            followers = int(u.get("follower_count") or 0)
            following = int(u.get("following_count") or 0)
            total_likes = int(u.get("total_favorited") or 0)
            profile_video_count = int(u.get("aweme_count") or 0)
            profile_private = bool(u.get("secret", 0))
    except Exception as exc:
        profile_error = str(exc)[:140]

    pub_items: list[dict[str, Any]] = []
    seen_pub_ids: set[str] = set()
    pub_error = ""
    try:
        cursor = "0"
        page_sz = max(12, min(35, max_posts))
        for _ in range(3):
            if len(pub_items) >= max_posts:
                break
            pub_resp = await client.get_user_posts(
                probe_account, handle, count=page_sz, max_cursor=cursor
            )
            raw_list = pub_resp.get("aweme_list") or (pub_resp.get("data") or {}).get("aweme_list") or []
            added_pg = 0
            if isinstance(raw_list, list):
                for x in raw_list:
                    if not isinstance(x, dict):
                        continue
                    aid_x = str(x.get("aweme_id") or "")
                    if aid_x and aid_x not in seen_pub_ids:
                        seen_pub_ids.add(aid_x)
                        pub_items.append(x)
                        added_pg += 1
            next_c = str(pub_resp.get("max_cursor") or "")
            if not pub_resp.get("has_more") or not next_c or next_c == cursor or added_pg == 0:
                break
            cursor = next_c
        pub_items = pub_items[:max_posts]
    except Exception as exc:
        pub_error = str(exc)[:140]

    public_ids = {str(it.get("aweme_id") or "") for it in pub_items if it.get("aweme_id")}

    # 2) Optional Deep Audit on owned authenticated session (Owner feed diff + /v1/post/get verify + 2048 + Studio)
    owner_only_items: list[dict[str, Any]] = []
    studio_penalties_map: dict[str, dict[str, Any]] = {}
    audit_health = "unknown"
    active_2048 = 0
    reviewing_2048 = 0
    audit_dict: dict[str, Any] | None = None

    if deep_audit and own_account is not None and own_account.authenticated and own_account.device:
        try:
            self_resp = await client.get_self_posts(own_account, count=max(12, min(35, max_posts)))
            self_list = self_resp.get("aweme_list") or (self_resp.get("data") or {}).get("aweme_list") or []
            if isinstance(self_list, list):
                for sit in self_list[:max_posts]:
                    if isinstance(sit, dict):
                        sid = str(sit.get("aweme_id") or "")
                        if sid and sid not in public_ids:
                            # Double-check via public `/v1/post/get` before flagging as hidden!
                            is_really_hidden = True
                            try:
                                gp = await client.get_post(probe_account, sid)
                                if isinstance(gp.get("aweme_detail"), dict) and gp["aweme_detail"].get("aweme_id"):
                                    is_really_hidden = False
                                    pub_items.append(gp["aweme_detail"])
                                    public_ids.add(sid)
                            except Exception:
                                is_really_hidden = True
                            if is_really_hidden:
                                owner_only_items.append(sit)
        except Exception:
            pass

        try:
            aud = await audit_account_restrictions(
                own_account,
                only_account_ineligible=False,
                verify_proxy_trace=False,
            )
            audit_health = aud.account_health
            active_2048 = sum(1 for v in aud.notice_violations if v.can_appeal or v.status_code == 1)
            reviewing_2048 = sum(1 for v in aud.notice_violations if v.status_code == 4)
            for pp in aud.post_penalties:
                if pp.appeal_status != 2 or pp.v2_can_appeal:
                    studio_penalties_map[str(pp.item_id)] = {
                        "reason_labels": list(pp.reason_labels),
                        "appeal_status": pp.appeal_status,
                        "appeal_status_label": pp.appeal_status_label,
                        "can_submit": pp.appeal_status == 0 or pp.v2_can_appeal,
                    }
            audit_dict = asdict(aud)
        except Exception:
            pass

    # 3) Inspect all public posts + any confirmed-hidden owner posts
    now_ts = time.time()
    posts_analyzed: list[dict[str, Any]] = []
    for it in pub_items:
        aid = str(it.get("aweme_id") or "")
        sp_info = studio_penalties_map.get(aid)
        posts_analyzed.append(
            inspect_aweme_shadow_signals(
                it,
                username=handle,
                is_public_visible=True,
                studio_flagged=sp_info is not None,
                studio_penalty_info=sp_info,
                zero_view_threshold_minutes=zero_view_threshold_minutes,
                now_ts=now_ts,
            )
        )
    for it in owner_only_items:
        aid = str(it.get("aweme_id") or "")
        sp_info = studio_penalties_map.get(aid)
        posts_analyzed.append(
            inspect_aweme_shadow_signals(
                it,
                username=handle,
                is_public_visible=False,
                studio_flagged=sp_info is not None,
                studio_penalty_info=sp_info,
                zero_view_threshold_minutes=zero_view_threshold_minutes,
                now_ts=now_ts,
            )
        )

    # 4) Honest account verdict (NO fake scores, NO screaming "SHADOWBAN" on 500-view live accounts!)
    total_posts = len(posts_analyzed)
    public_posts_count = len(pub_items)
    hidden_public_count = sum(1 for p in posts_analyzed if not p["is_public_visible"] and p["flags"]["private_status"] == 0)
    shadow_yes = sum(1 for p in posts_analyzed if p["shadow_code"] == "yes")
    shadow_suspect = sum(1 for p in posts_analyzed if p["shadow_code"] == "suspect")
    shadow_clean = sum(1 for p in posts_analyzed if p["shadow_code"] == "no")
    studio_flagged_count = len(studio_penalties_map)
    studio_can_submit_count = sum(1 for x in studio_penalties_map.values() if x.get("can_submit"))
    total_views = sum(p["views"] for p in posts_analyzed)
    zero_view_posts = sum(1 for p in posts_analyzed if p["views"] == 0)
    regions = sorted({p["region"] for p in posts_analyzed if p["region"]})
    post_urls = [p["url"] for p in posts_analyzed if p.get("url")]

    reasons: list[str] = []
    if active_2048 > 0:
        reasons.append(f"Активный огран 2048 ({active_2048})")
    if reviewing_2048 > 0:
        reasons.append(f"Апелляция 2048 на проверке ({reviewing_2048})")
    if hidden_public_count > 0:
        reasons.append(f"Скрыто из публички (3020004): {hidden_public_count} пост.")
    if shadow_yes > 0 and hidden_public_count == 0:
        reasons.append(f"Постов в тени: {shadow_yes}/{total_posts}")
    if shadow_suspect > 0:
        reasons.append(f"На проверке / подозр.: {shadow_suspect}/{total_posts}")
    if profile_private:
        reasons.append("Аккаунт приватный")

    if profile_error and not pub_items and not owner_only_items:
        verdict = "not_found"
        verdict_label = "💀 Не найден / Ошибка"
        reasons.append(profile_error)
    elif total_posts == 0:
        verdict = "no_public_posts"
        verdict_label = "💊 Нет публичных постов"
        if pub_error:
            reasons.append(pub_error)
    elif (
        active_2048 > 0
        or (public_posts_count == 0 and hidden_public_count > 0)
        or (total_posts > 0 and total_views == 0 and shadow_yes >= max(1, (total_posts + 1) // 2))
        or (total_posts > 0 and shadow_yes >= max(1, (total_posts + 1) // 2) and total_views < 10)
    ):
        verdict = "shadowbanned"
        verdict_label = "🚨 В тени" if active_2048 == 0 else "🚨 Огран 2048"
    elif shadow_yes > 0 or shadow_suspect > 0 or hidden_public_count > 0 or reviewing_2048 > 0:
        verdict = "partial_shadow"
        verdict_label = "⚠️ Частично скрыто" if hidden_public_count > 0 else "⚠️ Есть вопросы"
    else:
        verdict = "clean"
        verdict_label = "✅ Без тени"

    result: dict[str, Any] = {
        "account_id": account_id,
        "username": handle,
        "owned": own_account is not None,
        "copy_line": copy_line,
        "verdict": verdict,
        "verdict_label": verdict_label,
        "is_shadowbanned": verdict == "shadowbanned",
        "reasons": reasons,
        "followers": followers,
        "following": following,
        "total_likes": total_likes,
        "profile_video_count": profile_video_count,
        "public_posts_count": public_posts_count,
        "hidden_public_count": hidden_public_count,
        "total_posts": total_posts,
        "total_views": total_views,
        "zero_view_posts": zero_view_posts,
        "shadow_clean_posts": shadow_clean,
        "shadow_suspect_posts": shadow_suspect,
        "shadow_yes_posts": shadow_yes,
        "studio_flagged_count": studio_flagged_count,
        "studio_can_submit_count": studio_can_submit_count,
        "regions": regions,
        "post_urls": post_urls,
        "audit_health": audit_health,
        "active_2048": active_2048,
        "reviewing_2048": reviewing_2048,
        "checked_at": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
        "posts": posts_analyzed,
        "audit_dict": audit_dict,
    }
    result["telegram_text"] = format_account_telegram_report(result)
    return result


async def run_batch_shadowban_check(
    client: KintaroClient,
    store: StateStore,
    *,
    usernames: list[str],
    threads: int = 5,
    deep_audit: bool = False,
    max_posts: int = 12,
    zero_view_threshold_minutes: float = 20.0,
    on_start: Callable[[str], None] | None = None,
    on_done: Callable[[str, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Runs multi-threaded shadowban detection across a list of `@username`s or fleet accounts."""
    all_accs = store.list_accounts()
    probe_pool = [a for a in all_accs if a.authenticated and a.device]
    if not probe_pool:
        raise RuntimeError(
            "Нужен хотя бы 1 авторизованный аккаунт в базе (для подписанных запросов Kintaro API)."
        )

    own_by_low: dict[str, AccountRecord] = {}
    for a in all_accs:
        own_by_low[a.account_id.lower()] = a
        if a.username:
            own_by_low[a.username.lower()] = a

    clean_targets: list[str] = []
    seen_low: set[str] = set()
    for raw in usernames:
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        # Allow pasting full tiktok.com/@user links, shop lines, or @handles!
        if "tiktok.com/@" in s:
            s = s.split("tiktok.com/@", 1)[1].split("/", 1)[0].split("?", 1)[0]
        elif "|" in s:
            s = s.split("|", 1)[0]
        elif ":" in s and not s.startswith("http"):
            s = s.split(":", 1)[0]
        h = s.strip().lstrip("@")
        if h and h.lower() not in seen_low:
            seen_low.add(h.lower())
            clean_targets.append(h)

    if not clean_targets:
        raise ValueError("Список аккаунтов для проверки на тень пуст.")

    eff_threads = max(1, min(15, int(threads)))
    sem = asyncio.Semaphore(eff_threads)
    results_ordered: list[dict[str, Any] | None] = [None] * len(clean_targets)

    async def _one(idx: int, uname: str) -> None:
        async with sem:
            if on_start:
                on_start(uname)
            own = own_by_low.get(uname.lower())
            probe = probe_pool[idx % len(probe_pool)]
            try:
                res = await check_account_shadowban(
                    client,
                    probe_account=probe,
                    target_username=uname,
                    own_account=own,
                    deep_audit=deep_audit,
                    max_posts=max_posts,
                    zero_view_threshold_minutes=zero_view_threshold_minutes,
                )
            except Exception as exc:
                res = {
                    "account_id": own.account_id if own else uname,
                    "username": uname,
                    "owned": own is not None,
                    "copy_line": format_account_copy_line(own) if own else f"@{uname}",
                    "verdict": "not_found",
                    "verdict_label": "💀 Ошибка проверки",
                    "is_shadowbanned": False,
                    "reasons": [str(exc)[:140]],
                    "public_posts_count": 0,
                    "hidden_public_count": 0,
                    "total_posts": 0,
                    "total_views": 0,
                    "zero_view_posts": 0,
                    "shadow_clean_posts": 0,
                    "shadow_suspect_posts": 0,
                    "shadow_yes_posts": 0,
                    "studio_flagged_count": 0,
                    "studio_can_submit_count": 0,
                    "regions": [],
                    "post_urls": [],
                    "audit_health": "unknown",
                    "active_2048": 0,
                    "reviewing_2048": 0,
                    "checked_at": datetime.now(timezone.utc).strftime("%H:%M:%S UTC"),
                    "posts": [],
                    "telegram_text": f"❌ @{uname}: ошибка проверки ({str(exc)[:100]})",
                }
            # Strip large audit_dict before returning JSON payload
            res_public = {k: v for k, v in res.items() if k != "audit_dict"}
            results_ordered[idx] = res_public
            if on_done:
                on_done(uname, res)

    await asyncio.gather(*(_one(i, u) for i, u in enumerate(clean_targets)))
    final_list = [r for r in results_ordered if r is not None]

    counts = {
        "total": len(final_list),
        "clean": sum(1 for r in final_list if r["verdict"] == "clean"),
        "partial_shadow": sum(1 for r in final_list if r["verdict"] == "partial_shadow"),
        "shadowbanned": sum(1 for r in final_list if r["verdict"] == "shadowbanned"),
        "no_public_posts": sum(1 for r in final_list if r["verdict"] == "no_public_posts"),
        "not_found": sum(1 for r in final_list if r["verdict"] == "not_found"),
        "total_views": sum(int(r.get("total_views") or 0) for r in final_list),
        "total_posts": sum(int(r.get("total_posts") or 0) for r in final_list),
    }

    return {
        "ok": True,
        "threads": eff_threads,
        "deep_audit": deep_audit,
        "counts": counts,
        "results": final_list,
    }

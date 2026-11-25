from __future__ import annotations

import json
from typing import Any
from kintaro_auto.config import ResponseMappingSettings

KNOWN_DEVICE_HINT_KEYS = {
    "device_id",
    "install_id",
    "iid",
    "did",
    "openudid",
    "cdid",
    "clientudid",
    "ua",
    "user_agent",
    "cookies",
    "sessionid",
}


def get_by_path(data: Any, dotted_path: str) -> Any:
    cur = data
    for part in dotted_path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _normalize_device_candidate(val: Any) -> dict[str, Any] | None:
    if isinstance(val, str):
        stripped = val.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                parsed = json.loads(stripped)
                if isinstance(parsed, dict) and parsed:
                    return parsed
            except json.JSONDecodeError:
                return None
        return None
    if isinstance(val, dict) and val:
        return val
    return None


def extract_device_update(
    payload: Any,
    headers: dict[str, str],
    mapping: ResponseMappingSettings,
    *,
    is_device_endpoint: bool = False,
) -> dict[str, Any] | None:
    # 1. Check response header `x-device` first if server echoes/updates session there
    for hdr_name, hdr_val in headers.items():
        if hdr_name.lower() == "x-device":
            parsed = _normalize_device_candidate(hdr_val)
            if parsed is not None:
                return parsed

    if not isinstance(payload, dict):
        return None

    # 2. Check configured dotted paths (`device`, `data.device`, etc.)
    for path in mapping.device_keys:
        candidate = _normalize_device_candidate(get_by_path(payload, path))
        if candidate is not None:
            return candidate

    # 3. Check if `data` or root itself is a device state object
    data_sub = payload.get("data")
    if isinstance(data_sub, dict) and (
        KNOWN_DEVICE_HINT_KEYS.intersection(data_sub.keys())
        or (is_device_endpoint and len(data_sub) > 0)
    ):
        return data_sub

    if KNOWN_DEVICE_HINT_KEYS.intersection(payload.keys()) or (
        is_device_endpoint
        and len(payload) > 0
        and not {"status", "error", "detail", "message"}.issuperset(payload.keys())
    ):
        return payload

    return None


def extract_verify_object(
    payload: Any, mapping: ResponseMappingSettings
) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    for path in mapping.verify_keys:
        val = get_by_path(payload, path)
        if isinstance(val, dict) and val:
            return val
    # Check if challenge fields sit directly at root or `data`
    for container in (payload, payload.get("data")):
        if isinstance(container, dict) and "passport_ticket" in container:
            return container
    return None


def extract_feed_items(
    payload: Any, mapping: ResponseMappingSettings
) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        return []
    for path in mapping.feed_list_keys:
        val = get_by_path(payload, path)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
    return []


def extract_aweme_id(payload: Any, mapping: ResponseMappingSettings) -> str | None:
    if not isinstance(payload, dict):
        return None
    for path in mapping.aweme_id_keys:
        val = get_by_path(payload, path)
        if val is not None and str(val).strip():
            return str(val).strip()
    return None


def extract_author_username(item: dict[str, Any]) -> str | None:
    for path in (
        "author.unique_id",
        "author.username",
        "user.unique_id",
        "username",
        "unique_id",
    ):
        val = get_by_path(item, path)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def extract_play_count(
    payload: Any, mapping: ResponseMappingSettings
) -> int | None:
    if not isinstance(payload, dict):
        return None
    candidates = [payload]
    for nested_key in ("aweme_detail", "data.aweme_detail", "post", "data.post", "data"):
        sub = get_by_path(payload, nested_key)
        if isinstance(sub, dict):
            candidates.append(sub)
    for obj in candidates:
        for path in mapping.play_count_keys:
            val = get_by_path(obj, path)
            if isinstance(val, (int, float)):
                return int(val)
            if isinstance(val, str) and val.isdigit():
                return int(val)
    return None


def extract_first_music_id(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    for list_path in ("music", "data.music", "music_list", "data.music_list", "items", "data.items"):
        items = get_by_path(payload, list_path)
        if isinstance(items, list):
            for entry in items:
                if not isinstance(entry, dict):
                    continue
                for id_path in ("id_str", "id", "music_info.id_str", "music_info.id", "music.id_str", "music.id"):
                    mid = get_by_path(entry, id_path)
                    if mid is not None and str(mid).strip():
                        return str(mid).strip()
    return None

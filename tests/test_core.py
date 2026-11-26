from __future__ import annotations

import json
import random
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from PIL import Image

from kintaro_auto.appeals import (
    _extract_cookies_for_browser,
    _extract_query_from_schema,
    _resolve_playwright_http_proxy,
    build_tns_appeal_reason_json,
)
from kintaro_auto.captions import prepare_description_and_mentions
from kintaro_auto.client import KintaroClient
from kintaro_auto.config import AppConfig
from kintaro_auto.models import AccountRecord, UploadTask
from kintaro_auto.pipeline import AutomationPipeline
from kintaro_auto.storage import StateStore, parse_shop_account_line
from kintaro_auto.uniquifier import (
    detect_h264_encoder,
    probe_video,
    uniquify_photos,
    uniquify_video,
)


def test_unset_api_key_raises_clear_diagnostic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KINTARO_API_KEY", raising=False)
    cfg = AppConfig()
    with pytest.raises(RuntimeError, match="x-api-key"):
        cfg.resolve_api_key()


def test_account_validation_rejects_empty_proxy_or_bad_region() -> None:
    with pytest.raises(ValueError, match="proxy"):
        AccountRecord(account_id="a1", proxy="", region="US")

    with pytest.raises(ValueError, match="region"):
        AccountRecord(
            account_id="a1", proxy="socks5://u:p@1.2.3.4:1080", region="USA"
        )


def test_proxy_preserved_verbatim() -> None:
    raw_proxy = "socks5://bpuser-demo01:demoSecretToken_country-VN@residential-x.bpproxy.at:1002"
    acc = AccountRecord(
        account_id="demo_acc_01",
        proxy=raw_proxy,
        region="US",
    )
    assert acc.proxy == raw_proxy


def test_appeals_helpers_and_tns_reason_payload() -> None:
    pw_px = _resolve_playwright_http_proxy(
        "socks5://bpuser-demo01:demoSecretToken_country-VN@residential-x.bpproxy.at:1002"
    )
    assert pw_px["server"] == "http://residential-x.bpproxy.at:1000"
    assert pw_px["username"] == "bpuser-demo01"

    r_with_opts = json.loads(
        build_tns_appeal_reason_json(
            appeal_reasons_options=[{"id": 101, "text": "Original content"}],
            description_text="My own video",
        )
    )
    assert r_with_opts == {"type": 2, "options": [101], "description": "My own video"}

    r_plain = json.loads(
        build_tns_appeal_reason_json(
            appeal_reasons_options=None,
            description_text="My own video",
        )
    )
    assert r_plain == {"file_uri": "", "description": "My own video"}

    acc = AccountRecord(
        account_id="acc1",
        proxy="http://u:p@127.0.0.1:8080",
        region="US",
        device={"cookies": {"sessionid": "abc123", "sid_tt": "abc123"}},
    )
    cookies = _extract_cookies_for_browser(acc)
    assert any(c["name"] == "sessionid" and c["domain"] == ".tiktok.com" for c in cookies)


def test_outlook_hub_line_parser() -> None:
    raw = (
        "demo_user_001:DemoPass!123!:demo_inbox@outlook.com:"
        "[MailPass99:](https://MailPass99:rec_demo@polosmail.com)"
        "[rec_demo@polosmail.com](mailto:rec_demo@polosmail.com):RecPass!999:JBSWY3DPEHPK3PXP"
    )
    acc = parse_shop_account_line(raw, proxy="socks5://u:p@127.0.0.1:1080", region="US")
    assert acc.account_id == "demo_user_001"
    assert acc.username == "demo_user_001"
    assert acc.password == "DemoPass!123!"
    assert acc.email == "demo_inbox@outlook.com"
    assert acc.mail_password == "MailPass99"
    assert acc.recovery_email == "rec_demo@polosmail.com"
    assert acc.recovery_password == "RecPass!999"
    assert acc.mail_totp == "JBSWY3DPEHPK3PXP"
    assert acc.totp_token == ""
    assert acc.use_outlook_hub is True
    assert acc.auth_method == "email"


def test_inline_proxy_and_schema_unwrap() -> None:
    acc = parse_shop_account_line(
        "demo_u2|Secret!1|demo@example.com|JBSWY3DPEHPK3PXP|198.51.100.7:8080:pxuser:pxpass",
        proxy="http://fallback:pass@127.0.0.1:3128",
        region="US",
    )
    assert acc.username == "demo_u2"
    assert acc.totp_token == "JBSWY3DPEHPK3PXP"
    assert acc.proxy == "http://pxuser:pxpass@198.51.100.7:8080"

    inner_url, q = _extract_query_from_schema(
        "aweme://webview?url=https%3A%2F%2Fwww.tiktok.com%2Fviolation-appeal%3Fpunish_id%3D771001%26object_id%3D999%26punish_type%3D2048"
    )
    assert "violation-appeal" in inner_url
    assert q["punish_id"] == "771001"
    assert q["object_id"] == "999"
    assert q["punish_type"] == "2048"


def test_spintax_and_mentions_trailing_space() -> None:
    rng = random.Random(42)
    desc, mentions = prepare_description_and_mentions(
        "{Привет|Хай} @alice, смотри @bob",
        explicit_mentions=["@charlie", "alice"],
        rng=rng,
    )
    assert "@alice " in desc
    assert "@bob " in desc
    assert "@charlie " in desc
    assert mentions == ["alice", "bob", "charlie"]


def test_ffmpeg_video_uniquifier_produces_distinct_valid_mp4(tmp_path: Path) -> None:
    src_video = tmp_path / "source.mp4"
    enc = detect_h264_encoder()
    # Generate a tiny 1-second synthetic video with audio via ffmpeg
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=duration=1:size=360x640:rate=24",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-metadata",
            "comment=secret_source_tag",
            "-c:v",
            enc,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(src_video),
        ],
        check=True,
    )

    cfg = AppConfig()
    cfg.uniquifier.temp_dir = str(tmp_path / "out")

    m1 = uniquify_video(src_video, cfg.uniquifier, rng=random.Random(1))
    m2 = uniquify_video(src_video, cfg.uniquifier, rng=random.Random(2))

    assert m1.path.exists() and m2.path.exists()
    assert m1.sha256 != m2.sha256
    w, h, has_audio, _ = probe_video(m1.path)
    assert (w, h, has_audio) == (360, 640, True)

    # Verify Android MediaMuxer container profile (`mp42`, `VideoHandle`, no `Lavf` format tag)
    tags_proc = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags:stream_tags", "-of", "json", str(m1.path)],
        capture_output=True,
        text=True,
        check=True,
    )
    tags_info = json.loads(tags_proc.stdout)
    fmt_tags = (tags_info.get("format") or {}).get("tags") or {}
    assert fmt_tags.get("major_brand") == "mp42"
    assert "encoder" not in fmt_tags
    stream_handlers = [
        (s.get("tags") or {}).get("handler_name") for s in tags_info.get("streams", [])
    ]
    assert "VideoHandle" in stream_handlers
    assert "SoundHandle" in stream_handlers


def test_photo_uniquifier_strips_exif_and_varies_hash(tmp_path: Path) -> None:
    src_img = tmp_path / "slide.jpg"
    img = Image.new("RGB", (540, 960), color=(120, 80, 200))
    exif = img.getexif()
    exif[0x010E] = "IdentifyingCameraDescription"
    img.save(src_img, format="JPEG", exif=exif)

    cfg = AppConfig()
    cfg.uniquifier.temp_dir = str(tmp_path / "out_img")

    res1 = uniquify_photos([src_img], cfg.uniquifier, rng=random.Random(10))[0]
    res2 = uniquify_photos([src_img], cfg.uniquifier, rng=random.Random(20))[0]

    assert res1.sha256 != res2.sha256
    with Image.open(res1.path) as checked:
        assert checked.size == (540, 960)
        assert not checked.getexif()


async def test_full_pipeline_403_auth_warmup_upload_and_shadow_check(
    tmp_path: Path,
) -> None:
    # Create synthetic video
    src_video = tmp_path / "clip.mp4"
    enc = detect_h264_encoder()
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x568:d=0.6",
            "-c:v",
            enc,
            "-pix_fmt",
            "yuv420p",
            str(src_video),
        ],
        check=True,
    )

    calls: list[tuple[str, str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        hdrs = {k.lower(): v for k, v in request.headers.items()}
        assert hdrs.get("x-api-key") == "test_key_123"
        assert hdrs.get("x-proxy") == "socks5://user:pass@198.51.100.10:1080"
        calls.append((request.method, path, hdrs))

        if path == "/v1/device/register":
            assert "x-device" not in hdrs
            return httpx.Response(
                200,
                json={"device": {"device_id": "73999111222", "iid": "73999888777", "region": "US"}},
            )

        assert "x-device" in hdrs

        if path == "/v1/auth/signin/username":
            # Simulate 403 code challenge returning verify ticket + updated device
            return httpx.Response(
                403,
                json={
                    "device": {"device_id": "73999111222", "iid": "73999888777", "stage": "challenge"},
                    "verify": {
                        "passport_ticket": "TICKET_1",
                        "pseudo_id": "PSEUDO_1",
                        "device_redirect_info": "REDIRECT_1",
                        "challenge_type": "1",
                    },
                },
            )

        if path == "/v1/auth/signin/username/send_code":
            body = json.loads(request.content.decode())
            assert body["code"] == "654321"
            assert body["verify"]["passport_ticket"] == "TICKET_1"
            return httpx.Response(
                200,
                json={"device": {"device_id": "73999111222", "sessionid": "sess_active"}},
            )

        if path in (
            "/v1/device/update_hosts",
            "/v1/device/new_session",
            "/v1/account/token_beat",
            "/v1/account/self",
        ):
            return httpx.Response(200, json={"status_code": 0})

        if path == "/v1/feed/recommendations":
            return httpx.Response(
                200,
                json={
                    "aweme_list": [
                        {"aweme_id": "7500000000000000001", "author": {"unique_id": "creator1"}},
                        {"aweme_id": "7500000000000000002", "author": {"unique_id": "creator2"}},
                    ]
                },
            )

        if path in ("/v1/post/view", "/v1/post/like", "/v1/post/comments", "/v1/user/follow"):
            return httpx.Response(200, json={"status_code": 0})

        if path == "/v1/post/create/video":
            assert "multipart/form-data" in hdrs.get("content-type", "")
            assert b"video/mp4" in request.content
            return httpx.Response(200, json={"aweme_id": "7609998887776665554"})

        if path == "/v1/account/posts":
            return httpx.Response(
                200, json={"aweme_list": [{"aweme_id": "7609998887776665554"}]}
            )

        if path == "/v1/post/get":
            return httpx.Response(
                200,
                json={"aweme_detail": {"aweme_id": "7609998887776665554", "statistics": {"play_count": 0}}},
            )

        return httpx.Response(404, json={"error": "unhandled"})

    cfg = AppConfig()
    cfg.state_dir = str(tmp_path / "state")
    cfg.api.api_key = "test_key_123"
    cfg.api.save_raw_responses = False
    cfg.uniquifier.temp_dir = str(tmp_path / "tmp_media")
    cfg.warmup.feed_rounds_min = 1
    cfg.warmup.feed_rounds_max = 1
    cfg.warmup.views_per_round_min = 2
    cfg.warmup.views_per_round_max = 2
    cfg.warmup.action_delay_seconds_min = 0.0
    cfg.warmup.action_delay_seconds_max = 0.0
    cfg.warmup.pre_upload_pause_seconds_min = 0.0
    cfg.warmup.pre_upload_pause_seconds_max = 0.0

    store = StateStore(cfg.state_dir)
    acc = AccountRecord(
        account_id="acc_test",
        proxy="socks5://user:pass@198.51.100.10:1080",
        region="US",
        auth_method="username",
        username="cat_tester",
        password="Secret123!",
    )
    store.save_account(acc)

    async def fast_sleep(_: float) -> None:
        return None

    async with KintaroClient(cfg, transport=httpx.MockTransport(handler)) as client:
        pipeline = AutomationPipeline(
            cfg, store, client, sleep_fn=fast_sleep, rng=random.Random(7)
        )

        # 1. Authenticate (handles 403 challenge via callback)
        authed = await pipeline.ensure_authenticated(acc, code_provider=lambda _: "654321")
        assert authed.authenticated is True
        assert isinstance(authed.device, dict)
        assert authed.device.get("sessionid") == "sess_active"

        # 2. Fresh login cooldown blocks immediate upload unless aged or forced
        ok, reason = pipeline.check_posting_eligibility(authed)
        assert ok is False
        assert "Свежая сессия" in reason

        # Simulate aged session (2 hours old)
        authed.first_authenticated_at = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        store.save_account(authed)

        # 3. Execute upload task (triggers warm-up + ffmpeg uniquifier + multipart upload)
        task = UploadTask(
            task_id="t1",
            account_id="acc_test",
            media_type="video",
            video_path=str(src_video),
            description="{Тест|Проверка} @friend",
        )
        updated_acc, post_rec = await pipeline.execute_upload_task(task)
        assert post_rec.aweme_id == "7609998887776665554"
        assert post_rec.description.endswith("@friend ")
        # Temp uniquified video must be cleaned up
        assert list(Path(cfg.uniquifier.temp_dir).glob("*.mp4")) == []

        # 4. Simulate post being 60 minutes old with 0 views -> shadowban flagged
        updated_acc.upload_history[0].uploaded_at = (
            datetime.now(timezone.utc) - timedelta(minutes=60)
        ).isoformat()
        store.save_account(updated_acc)

        reports = await pipeline.check_account_shadows(
            updated_acc, zero_view_threshold_minutes=45.0
        )
        assert len(reports) == 1
        assert reports[0].suspected_shadowban is True
        assert reports[0].play_count == 0

    called_paths = [p for _, p, _ in calls]
    assert "/v1/device/register" in called_paths
    assert "/v1/feed/recommendations" in called_paths
    assert "/v1/post/view" in called_paths
    assert "/v1/post/create/video" in called_paths


async def test_web_dashboard_index_and_state(tmp_path: Path) -> None:
    from kintaro_auto.web import create_web_app

    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        f'state_dir = "{tmp_path / "state"}"\n[api]\napi_key = "demo_key"\n',
        encoding="utf-8",
    )
    web_app = create_web_app(cfg_file)
    transport = httpx.ASGITransport(app=web_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r_html = await c.get("/")
        assert r_html.status_code == 200
        assert "KINTARO FARM OS" in r_html.text

        r_js = await c.get("/static/app.js")
        assert r_js.status_code == 200

        r_st = await c.get("/api/state")
        assert r_st.status_code == 200
        assert "accounts" in r_st.json()

        r_prev = await c.post(
            "/api/preview-lines",
            json={
                "lines_text": "demo|pw|d@x.com|JBSWY3DPEHPK3PXP\nu2:pw2:m@outlook.com:mpw:r@polosmail.com:rpw:JBSWY3DPEHPK3PXP",
                "proxies_text": "198.51.100.1:8080:u:p",
                "region": "US",
            },
        )
        assert r_prev.status_code == 200
        assert r_prev.json()["valid_count"] == 2

        r_spin = await c.post(
            "/api/spintax-preview",
            json={"template": "{Привет|Хай} @friend #fyp"},
        )
        assert r_spin.status_code == 200
        assert len(r_spin.json()["variants"]) == 4

        r_cfg = await c.get("/api/config")
        assert r_cfg.status_code == 200
        assert r_cfg.json()["has_api_key"] is True


async def test_ten_account_bans_not_collapsed_and_studio_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    from kintaro_auto.appeals import audit_account_restrictions, remove_account_restrictions

    notices_10 = [
        {
            "nid_str": f"n{i}",
            "create_time": 1795642000 + i,
            "template_notice": {
                "notice": {"title": {"title": "Account ineligible for recommendation"}},
                "schema_url": (
                    f"https://www.tiktok.com/violation-appeal?object_id=7071348182533866497"
                    f"&object_type=4&punish_id=77122241666736688{i:02d}&punish_type=2048"
                ),
            },
        }
        for i in range(10)
    ]
    submitted_studio: list[str] = []
    submitted_notices: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/cdn-cgi/trace":
            return httpx.Response(200, text="ip=1.2.3.4\nloc=VN\n")
        if p == "/tiktokstudio/api/web/user":
            return httpx.Response(
                200,
                json={"userId": "7071348182533866497", "userBaseInfo": {"UserProfile": {"UserBase": {"UniqId": "demo10", "Region": {"Region": "US", "RegistrationCountry": "id"}}}}},
            )
        if p == "/aweme/v1/notice/count/":
            return httpx.Response(200, json={"groups_in_filters": [{"filter_type": 51, "groups": [47]}]})
        if p == "/aweme/v1/notice/multi/":
            return httpx.Response(200, json={"notice_lists": [{"group": 47, "notice_list": notices_10}]})
        if p == "/api/violation/v1/get_violation_detail":
            vid = req.url.params.get("ViolationId", "")
            status = 4 if vid in submitted_notices else 1
            return httpx.Response(
                200,
                json={"BaseResp": {"StatusCode": 0}, "CanAppeal": status == 1, "ViolationInfo": {"Status": status}},
            )
        if p == "/tiktok/creator/manage/item_list/v1/":
            return httpx.Response(200, json={"item_list": [{"item_id": "999000111", "create_time": 1795642000, "desc": "clip", "play_count": 0}]})
        if p == "/mod/v1/getPenaltyDetails/":
            return httpx.Response(200, json={"appeal_status": 0, "penalties": [{"issuer": 1, "penalty_id": {"id": 55}}]})
        if p == "/api/moderation/penalties/gettransparencypagecontent":
            return httpx.Response(200, json={"showAppealBtn": True, "appealDetails": [{"x": 1}]})
        if p == "/tiktok/v1/appeal/reason/":
            return httpx.Response(200, json={"appeal_reasons": [{"code": 1}]})
        if p == "/api/violation/v1/commit_appeal":
            body = json.loads(req.content.decode())
            submitted_notices.append(body["ViolationId"])
            return httpx.Response(200, json={"BaseResp": {"StatusCode": 0}})
        if p in ("/mod/v1/submitAppeals", "/api/moderation/penalties/submitappeal"):
            submitted_studio.append(p)
            return httpx.Response(200, json={"status_code": 0})
        return httpx.Response(200, json={})

    orig_client = httpx.AsyncClient

    def patched_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("proxy", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return orig_client(*args, **kwargs)

    monkeypatch.setattr("kintaro_auto.appeals.httpx.AsyncClient", patched_client)

    acc = AccountRecord(
        account_id="demo10",
        proxy="socks5://u:p@127.0.0.1:1080",
        region="US",
        device={"cookies": {"sessionid": "s1", "store-idc": "alisg"}},
    )
    aud = await audit_account_restrictions(acc)
    assert len(aud.notice_violations) == 10
    assert aud.post_penalties[0].appeal_status == 0

    summary = await remove_account_restrictions(acc, stagger_appeals=False)
    assert summary.notice_appeals_verified == 10
    assert summary.post_appeals_verified == 0
    assert submitted_studio == []


def test_all_photo_uniquifier_presets_and_mp4_free_atom(tmp_path: Path) -> None:
    from kintaro_auto.uniquifier import (
        PHOTO_UNIQUIFIER_PRESETS,
        uniquify_mp4_free_atom,
        uniquify_photo_bytes,
    )

    src = tmp_path / "slide.jpg"
    Image.new("RGB", (360, 640), (24, 52, 130)).save(src, format="JPEG", quality=92)
    raw = src.read_bytes()

    hashes = set()
    for mode in ("minimal_snow", "minimal", "stealth", "medium", "boosted", "ultra_hard"):
        assert mode in PHOTO_UNIQUIFIER_PRESETS
        out = uniquify_photo_bytes(raw, mode=mode, rng=random.Random(100))
        assert len(out) > 1000
        hashes.add(out[:64])
    assert len(hashes) >= 5

    # Distinct copies in minimal_snow produce distinct files
    cfg = AppConfig()
    cfg.uniquifier.temp_dir = str(tmp_path / "out")
    cfg.uniquifier.photo_mode = "minimal_snow"
    m1 = uniquify_photos([src], cfg.uniquifier, rng=random.Random(1))[0]
    m2 = uniquify_photos([src], cfg.uniquifier, rng=random.Random(2))[0]
    assert m1.sha256 != m2.sha256

    mp4_a = uniquify_mp4_free_atom(b"\x00\x00\x00\x18ftypmp42", rng=random.Random(1))
    mp4_b = uniquify_mp4_free_atom(b"\x00\x00\x00\x18ftypmp42", rng=random.Random(2))
    assert mp4_a != mp4_b and b"free" in mp4_a


def test_hashtag_randomizer_sticky_proxy_geo_and_live_totp() -> None:
    from kintaro_auto.captions import randomize_hashtags
    from kintaro_auto.models import (
        DEVICE_GEO_PROFILES,
        allocate_sticky_proxy,
        apply_device_geo_profile,
    )
    from kintaro_auto.spy import generate_live_totp

    used: set[str] = set()
    tags = "#piratetoken #piratetokenshop #blackknight #pirateshop #fyp"
    perms = {randomize_hashtags(tags, used_captions=used, rng=random.Random(i)) for i in range(6)}
    assert len(perms) == 6

    desc, _ = prepare_description_and_mentions("{A|B} {RAND4} #a #b #c", rng=random.Random(7), shuffle_tags=True)
    assert "{RAND4}" not in desc

    assert len(DEVICE_GEO_PROFILES) >= 21
    dev = apply_device_geo_profile({"cookies": {"sessionid": "x"}}, "IT", app_locale="it_IT")
    assert dev["carrier"] == "TIM" and dev["locale"] == "it_IT" and dev["language"] == "it"

    raw_px = "socks5://bpuser-demo:sec_country-VN@residential-x.bpproxy.at:1002"
    pinned_same = allocate_sticky_proxy(raw_px, target_country="", lifetime="60m")
    assert "_country-VN_session-" in pinned_same and "_lifetime-60m" in pinned_same
    pinned_us = allocate_sticky_proxy(raw_px, target_country="US", lifetime="60m", preserve_existing_session=False)
    assert "_country-US_session-" in pinned_us

    totp = generate_live_totp("JBSWY3DPEHPK3PXP", timestamp=1700000000)
    assert len(totp["code"]) == 6 and totp["code"].isdigit() and 1 <= totp["remaining_sec"] <= 30


@pytest.mark.asyncio
async def test_cold_restart_clean_posts_and_anti_shadow_campaign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kintaro_auto.models import CampaignConfig

    slide1 = tmp_path / "s1.jpg"
    slide2 = tmp_path / "s2.jpg"
    Image.new("RGB", (320, 560), (20, 40, 120)).save(slide1, format="JPEG")
    Image.new("RGB", (320, 560), (200, 160, 20)).save(slide2, format="JPEG")

    calls: list[str] = []
    deleted_ids: list[str] = []
    created_posts: list[str] = []

    def kt_handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        calls.append(p)
        if p == "/v1/account/posts":
            if not deleted_ids and not created_posts:
                return httpx.Response(200, json={"aweme_list": [{"aweme_id": "111000"}]})
            return httpx.Response(
                200,
                json={"aweme_list": [{"aweme_id": aid, "statistics": {"play_count": 42}} for aid in created_posts]},
            )
        if p == "/v1/post/delete":
            body = json.loads(req.content.decode())
            deleted_ids.append(body["aweme_id"])
            return httpx.Response(200, json={"status_code": 0})
        if p == "/v1/music/search":
            return httpx.Response(
                200,
                json={"music": [{"id_str": f"700000{i}", "title": f"Fortnite Track {i}", "author": "FN", "duration": 30} for i in range(6)]},
            )
        if p == "/v1/post/create/photo":
            aid = f"771224000{len(created_posts) + 1}"
            created_posts.append(aid)
            return httpx.Response(200, json={"aweme": {"aweme_id": aid, "region": "US"}})
        if p in ("/v1/device/new_session", "/v1/device/update_hosts", "/v1/device/report", "/v1/account/token_beat", "/v1/account/self"):
            return httpx.Response(200, json={"status_code": 0})
        return httpx.Response(200, json={})

    # Patch appeals to fast clean response
    def appeal_handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/tiktokstudio/api/web/user":
            return httpx.Response(200, json={"userId": "7071", "userBaseInfo": {"UserProfile": {"UserBase": {"UniqId": "camp_u1"}}}})
        if p == "/aweme/v1/notice/multi/":
            return httpx.Response(200, json={"notice_lists": [{"group": 47, "notice_list": []}]})
        return httpx.Response(200, json={})

    orig_async_client = httpx.AsyncClient

    def patched_appeal_client(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        if "transport" not in kwargs:
            kwargs.pop("proxy", None)
            kwargs["transport"] = httpx.MockTransport(appeal_handler)
        return orig_async_client(*args, **kwargs)

    monkeypatch.setattr("kintaro_auto.appeals.httpx.AsyncClient", patched_appeal_client)

    cfg = AppConfig(state_dir=str(tmp_path / "state"))
    cfg.api.api_key = "demo-key"
    cfg.api.save_raw_responses = False
    cfg.uniquifier.temp_dir = str(tmp_path / "tmp_media")
    store = StateStore(cfg.state_dir)
    acc = AccountRecord(
        account_id="camp_u1",
        username="camp_u1",
        proxy="socks5://u:p@127.0.0.1:1080",
        region="US",
        authenticated=True,
        device={
            "cookies": {"sessionid": "s1", "store-idc": "alisg"},
            "session_ids": {"session": "old-sess-1111"},
            "counters": {"sessions": 1},
        },
    )
    store.save_account(acc)

    async def _fast_sleep(_sec: float) -> None:
        return None

    async with KintaroClient(cfg, transport=httpx.MockTransport(kt_handler)) as client:
        pipeline = AutomationPipeline(cfg, store, client, sleep_fn=_fast_sleep, rng=random.Random(42))
        camp = CampaignConfig(
            account_ids=["camp_u1"],
            media_type="photo",
            media_paths=[str(slide1), str(slide2)],
            description="#piratetoken #piratetokenshop #blackknight #pirateshop #fyp",
            copies_count=6,
            threads=5,
            restart_every_n=3,
            delay_min_sec=0.0,
            delay_max_sec=0.0,
            photo_mode="minimal_snow",
            auto_music_query="Fortnite",
            delete_old_posts=True,
            auto_appeal_2048=True,
            appeal_post_wait_sec=0.0,
            appeal_quiet_window_sec=0.0,
            sort_to_folders=True,
        )
        results = await pipeline.run_anti_shadow_campaign(camp)

    assert len(results) == 1
    r0 = results[0]
    assert r0.status == "completed"
    assert r0.posts_published == 6
    assert r0.deleted_old_posts == 1
    assert deleted_ids == ["111000"]
    assert r0.folder_key == "clean"
    assert r0.initial_views == 6 * 42
    # Cold TikTok restart ran after post #3
    assert "/v1/device/report" in calls
    assert "/v1/account/token_beat" in calls
    reloaded = store.load_account("camp_u1")
    assert isinstance(reloaded.device, dict)
    assert reloaded.device["counters"]["sessions"] == 2
    assert reloaded.device["session_ids"]["session"] != "old-sess-1111"
    assert (tmp_path / "state" / "sorted_accounts" / "clean" / "clean_accounts.txt").exists()


def test_web_v4_presets_uniquifier_preview_and_totp(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from kintaro_auto.web import create_web_app

    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'state_dir = "{tmp_path / "state"}"\n', encoding="utf-8")
    app = create_web_app(cfg_path)
    tc = TestClient(app)

    r_pre = tc.get("/api/presets")
    assert r_pre.status_code == 200
    body_pre = r_pre.json()
    assert "minimal_snow" in body_pre["photo_presets"]
    assert any(p["id"] == "us_6posts_minimal_snow" for p in body_pre["campaign_presets"])

    img_buf = tmp_path / "t.jpg"
    Image.new("RGB", (240, 420), (30, 80, 180)).save(img_buf, format="JPEG")
    r_prev = tc.post(
        "/api/uniquifier/preview",
        data={"photo_mode": "minimal_snow"},
        files={"file": ("t.jpg", img_buf.read_bytes(), "image/jpeg")},
    )
    assert r_prev.status_code == 200
    assert r_prev.json()["after_data_url"].startswith("data:image/jpeg;base64,")

    r_totp = tc.post("/api/totp/generate", json={"secret": "JBSWY3DPEHPK3PXP"})
    assert r_totp.status_code == 200
    assert len(r_totp.json()["code"]) == 6


def test_web_v5_media_folders_exports_autopilot_and_system_info(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from kintaro_auto.appeals import sort_account_into_folder
    from kintaro_auto.platform_utils import configure_windows_runtime, find_chromium_executable
    from kintaro_auto.web import create_web_app

    configure_windows_runtime()
    _ = find_chromium_executable()

    cfg_path = tmp_path / "config.toml"
    sdir = tmp_path / "state"
    cfg_path.write_text(f'state_dir = "{sdir}"\n', encoding="utf-8")
    app = create_web_app(cfg_path)
    tc = TestClient(app)

    # 1. System Info
    r_sys = tc.get("/api/system/info")
    assert r_sys.status_code == 200 and r_sys.json()["ok"] is True

    # 2. Media library upload / list / delete
    img = tmp_path / "m1.jpg"
    Image.new("RGB", (200, 300), (10, 20, 30)).save(img, format="JPEG")
    r_up = tc.post("/api/media/upload", files=[("files", ("m1.jpg", img.read_bytes(), "image/jpeg"))])
    assert r_up.status_code == 200 and "m1.jpg" in r_up.json()["saved"]
    r_file = tc.get("/api/media/file/m1.jpg")
    assert r_file.status_code == 200

    # 3. Raw x-device session import + ZIP/JSONL + CSV exports + Sorted Folders ZIP
    raw_sess = json.dumps({"username": "v5demo", "region": "US", "device": {"device_id": "123", "cookies": {"sessionid": "abc"}}})
    r_imp = tc.post("/api/sessions/import-raw", json={"raw_text": raw_sess, "default_proxy": "http://u:p@127.0.0.1:8080"})
    assert r_imp.status_code == 200 and r_imp.json()["imported"] == ["v5demo"]

    assert tc.get("/api/sessions/export/zip").status_code == 200
    assert tc.get("/api/sessions/export/jsonl").status_code == 200
    r_csv = tc.get("/api/analytics/export.csv")
    assert r_csv.status_code == 200 and "v5demo" in r_csv.text

    acc_obj = AccountRecord(account_id="v5demo", username="v5demo", proxy="http://u:p@127.0.0.1:8080", region="US")
    sort_account_into_folder(sdir, "clean", acc_obj)
    assert tc.get("/api/folders/clean/export?fmt=txt").status_code == 200
    assert tc.get("/api/folders/clean/export?fmt=zip").status_code == 200

    # 4. Auto-Pilot scheduler add / list / delete
    r_ap = tc.post("/api/autopilot/add", json={"action": "audit_all", "interval_minutes": 45})
    assert r_ap.status_code == 200
    jid = r_ap.json()["job"]["id"]
    assert any(j["id"] == jid for j in tc.get("/api/autopilot").json()["jobs"])
    assert tc.delete(f"/api/autopilot/{jid}").status_code == 200


async def test_delete_all_posts_studio_and_kintaro_union_and_upload_cleanup_appeal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kintaro_auto.appeals import AccountAppealSummary
    from kintaro_auto.models import UploadedPostRecord

    src_img = tmp_path / "post.jpg"
    Image.new("RGB", (360, 640), (45, 90, 140)).save(src_img, format="JPEG")

    # Studio returns a private post ("studio_priv_1") + shared post ("shared_2")
    async def fake_studio_ids(_acc: AccountRecord, *, max_posts: int = 200) -> list[str]:
        return ["studio_priv_1", "shared_2"]

    events: list[str] = []

    async def fake_settle(_acc: AccountRecord, **_kwargs: Any) -> AccountAppealSummary:
        events.append("appeal_settle")
        return AccountAppealSummary(account_id=_acc.account_id, notice_appeals_verified=3)

    monkeypatch.setattr("kintaro_auto.pipeline.list_studio_post_ids", fake_studio_ids)
    monkeypatch.setattr("kintaro_auto.pipeline.wait_and_appeal_settle", fake_settle)

    deleted_order: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/v1/account/posts":
            cursor = req.url.params.get("max_cursor", "0")
            if cursor == "0":
                return httpx.Response(
                    200,
                    json={
                        "aweme_list": [{"aweme_id": "shared_2"}, {"aweme_id": "mobile_3"}],
                        "has_more": 1,
                        "max_cursor": "cursor_page_2",
                    },
                )
            return httpx.Response(
                200,
                json={"aweme_list": [{"aweme_id": "mobile_4"}], "has_more": 0, "max_cursor": "0"},
            )
        if p == "/v1/post/delete":
            body = json.loads(req.content.decode())
            aid = body["aweme_id"]
            deleted_order.append(aid)
            events.append(f"delete:{aid}")
            return httpx.Response(200, json={"status_code": 0, "status_msg": ""})
        if p == "/v1/post/create/photo":
            events.append("upload_photo")
            return httpx.Response(200, json={"aweme_id": "fresh_new_999"})
        return httpx.Response(200, json={"status_code": 0})

    cfg = AppConfig()
    cfg.state_dir = str(tmp_path / "state")
    cfg.api.api_key = "demo_key"
    cfg.api.save_raw_responses = False
    cfg.uniquifier.temp_dir = str(tmp_path / "tmp_media")
    store = StateStore(cfg.state_dir)

    acc = AccountRecord(
        account_id="clean_acc",
        proxy="socks5://u:p@127.0.0.1:1080",
        region="US",
        authenticated=True,
        device={"cookies": {"sessionid": "s1"}},
        upload_history=[
            UploadedPostRecord(aweme_id="shared_2", media_type="photo", source_files=[], description="old"),
            UploadedPostRecord(aweme_id="keep_me", media_type="photo", source_files=[], description="keep"),
        ],
    )
    store.save_account(acc)

    async def fast_sleep(_: float) -> None:
        return None

    async with KintaroClient(cfg, transport=httpx.MockTransport(handler)) as client:
        pipeline = AutomationPipeline(cfg, store, client, sleep_fn=fast_sleep, rng=random.Random(3))

        # 1) Dry run finds all 4 unique posts across Studio + paginated Kintaro (2 pages), deletes 0
        dry_cnt = await pipeline.delete_all_account_posts(acc, keep_ids={"mobile_4"}, dry_run=True)
        assert dry_cnt == 3
        assert deleted_order == []

        # 2) execute_upload_task with delete_old_posts=True & appeal_after_upload=True runs in exact order
        task = UploadTask(
            task_id="up_clean_1",
            account_id="clean_acc",
            media_type="photo",
            photo_paths=[str(src_img)],
            description="Fresh #fyp",
        )
        updated_acc, rec = await pipeline.execute_upload_task(
            task,
            force_cooldown=True,
            skip_warmup=True,
            delete_old_posts=True,
            appeal_after_upload=True,
            settle_after_upload=True,
        )

    assert deleted_order == ["studio_priv_1", "shared_2", "mobile_3", "mobile_4"]
    assert rec.aweme_id == "fresh_new_999"
    assert events == [
        "delete:studio_priv_1",
        "delete:shared_2",
        "delete:mobile_3",
        "delete:mobile_4",
        "upload_photo",
        "appeal_settle",
    ]
    # Old deleted ID pruned; kept + new post remain in upload_history
    remaining_hist = [p.aweme_id for p in updated_acc.upload_history]
    assert "shared_2" not in remaining_hist
    assert "keep_me" in remaining_hist
    assert "fresh_new_999" in remaining_hist


def test_copy_export_formats_and_sorted_folders(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from kintaro_auto.appeals import sort_account_into_folder
    from kintaro_auto.models import UploadedPostRecord
    from kintaro_auto.storage import StateStore, format_account_copy_line, parse_shop_account_line
    from kintaro_auto.web import create_web_app

    sdir = tmp_path / "state"
    cfg_path = tmp_path / "config.toml"
    cfg_path.write_text(f'state_dir = "{sdir}"\n', encoding="utf-8")

    store = StateStore(sdir)
    raw_line_1 = "u_copy_1:ttPass!1:mail1@outlook.com:mPass1:rec1@vargosmail.com:rPass1:BASE32SECRET1"
    acc1 = parse_shop_account_line(raw_line_1, proxy="socks5://u:p@127.0.0.1:1080", region="US")
    acc1.authenticated = True
    acc1.device = {"cookies": {"sessionid": "s1"}}
    acc1.upload_history = [
        UploadedPostRecord(aweme_id="7700111", media_type="photo", source_files=[], description="#fyp"),
        UploadedPostRecord(aweme_id="7700112", media_type="photo", source_files=[], description="#fyp"),
    ]
    store.save_account(acc1)

    acc2 = AccountRecord(
        account_id="u_copy_2",
        username="u_copy_2",
        password="p2",
        email="u2@example.com",
        totp_token="TOTP222222222222",
        proxy="socks5://u:p@127.0.0.1:1080",
        region="US",
        authenticated=True,
        device={"cookies": {"sessionid": "s2"}},
    )
    store.save_account(acc2)
    sort_account_into_folder(sdir, "clean", acc1)

    assert format_account_copy_line(acc1) == raw_line_1
    assert format_account_copy_line(acc2) == "u_copy_2|p2|u2@example.com|TOTP222222222222"

    app = create_web_app(cfg_path)
    tc = TestClient(app)

    # Uploaded scope returns only acc1 (which has upload_history)
    r_lines = tc.get("/api/accounts/copy-export?scope=uploaded&fmt=lines")
    assert r_lines.status_code == 200
    body_l = r_lines.json()
    assert body_l["count"] == 1
    assert body_l["text"] == raw_line_1

    r_unames = tc.get("/api/accounts/copy-export?scope=uploaded&fmt=usernames")
    assert r_unames.json()["text"] == "@u_copy_1"

    r_urls = tc.get("/api/accounts/copy-export?scope=uploaded&fmt=urls")
    assert r_urls.json()["count"] == 2
    assert "https://www.tiktok.com/@u_copy_1/photo/7700111" in r_urls.json()["text"]

    # Selected scope returns both in order
    r_sel = tc.get("/api/accounts/copy-export?scope=selected&fmt=lines&ids=u_copy_2,u_copy_1")
    assert r_sel.json()["count"] == 2
    assert r_sel.json()["text"].splitlines()[0] == "u_copy_2|p2|u2@example.com|TOTP222222222222"

    # Clean sorted folder copy
    r_clean = tc.get("/api/accounts/copy-export?scope=clean&fmt=lines")
    assert r_clean.json()["count"] == 1
    assert r_clean.json()["text"] == raw_line_1


async def test_unban_wave_queue_manager_and_deferred_campaign_handoff(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    from kintaro_auto.appeals import AccountAppealSummary, AccountAuditResult
    from kintaro_auto.models import CampaignConfig
    from kintaro_auto.studio_ops import UnbanWaveQueueManager

    sdir = tmp_path / "state"
    store = StateStore(sdir)
    for idx in (1, 2):
        store.save_account(
            AccountRecord(
                account_id=f"uq_{idx}",
                username=f"uq_{idx}",
                proxy="socks5://u:p@127.0.0.1:1080",
                region="US",
                authenticated=True,
                device={"cookies": {"sessionid": f"s{idx}"}},
            )
        )

    processed: list[str] = []

    async def fake_settle(acc: AccountRecord, **kwargs: Any) -> AccountAppealSummary:
        cb = kwargs.get("progress_cb")
        if cb:
            cb({"appeals_sent": 2, "active_remaining": 0, "reviewing_count": 0, "idle_sec": 5, "quiet_sec": 5, "elapsed_sec": 5})
        processed.append(acc.account_id)
        aud = AccountAuditResult(
            account_id=acc.account_id,
            username=acc.username or acc.account_id,
            uniq_id=acc.username or acc.account_id,
            nickname=acc.username or acc.account_id,
            avatar_url="",
            uid="100",
            reg_country="US",
            current_region="US",
            idc_gateway="https://api16-normal-c-alisg.tiktokv.com",
            proxy_ip="1.2.3.4",
            proxy_country="US",
            account_health="eligible",
        )
        return AccountAppealSummary(account_id=acc.account_id, audit=aud, notice_appeals_verified=2)

    monkeypatch.setattr("kintaro_auto.studio_ops.wait_and_appeal_settle", fake_settle)

    mgr = UnbanWaveQueueManager(store, sdir)
    res = mgr.enqueue(["uq_1", "uq_2"], threads=2, wait_settle=True, quiet_seconds=0.0, poll_interval_seconds=0.05)
    assert res["added"] == ["uq_1", "uq_2"]
    for _ in range(50):
        if not mgr.snapshot()["running"]:
            break
        await asyncio.sleep(0.02)

    snap = mgr.snapshot()
    assert snap["running"] is False
    assert sorted(processed) == ["uq_1", "uq_2"]
    assert snap["counts"]["clean"] == 2
    assert snap["counts"]["appeals_total"] == 4
    assert (sdir / "sorted_accounts" / "clean" / "clean_accounts.txt").is_file()

    # Also verify CampaignConfig.defer_post_appeal_to_queue hands off instead of inline Stage-3 wait
    slide = tmp_path / "s.jpg"
    Image.new("RGB", (320, 480), (40, 80, 120)).save(slide, format="JPEG")

    def kt_handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/post/create/photo":
            return httpx.Response(200, json={"aweme_id": "88001"})
        if req.url.path == "/v1/account/posts":
            return httpx.Response(200, json={"aweme_list": [{"aweme_id": "88001", "statistics": {"play_count": 12}}]})
        return httpx.Response(200, json={"status_code": 0})

    inline_settle_called = False

    async def fail_if_inline_called(*_a: Any, **_kw: Any) -> AccountAppealSummary:
        nonlocal inline_settle_called
        inline_settle_called = True
        raise AssertionError("Inline Stage-3 wait_and_appeal_settle must be skipped when deferred to queue!")

    monkeypatch.setattr("kintaro_auto.pipeline.wait_and_appeal_settle", fail_if_inline_called)
    handed_off: list[str] = []

    cfg = AppConfig(state_dir=str(sdir))
    cfg.api.api_key = "demo-key"
    cfg.api.save_raw_responses = False
    cfg.uniquifier.temp_dir = str(tmp_path / "tmp_m")

    async def fast_sleep(_: float) -> None:
        return None

    async with KintaroClient(cfg, transport=httpx.MockTransport(kt_handler)) as client:
        pipe = AutomationPipeline(cfg, store, client, sleep_fn=fast_sleep, rng=random.Random(7))
        camp = CampaignConfig(
            account_ids=["uq_1"],
            media_type="photo",
            media_paths=[str(slide)],
            copies_count=1,
            threads=1,
            delay_min_sec=0.0,
            delay_max_sec=0.0,
            delete_old_posts=False,
            auto_music_query="",
            auto_appeal_2048=True,
            appeal_pre_upload=False,
            appeal_inter_post=False,
            appeal_post_upload=True,
            defer_post_appeal_to_queue=True,
            appeal_queue_threads=3,
        )
        out = await pipe.run_anti_shadow_campaign(
            camp,
            on_account_uploaded=lambda acc, _r: handed_off.append(acc.account_id),
        )

    assert inline_settle_called is False
    assert handed_off == ["uq_1"]
    assert out[0].folder_key == "queued_unban"
    assert out[0].posts_published == 1


async def test_pro_shadowban_checker_detects_real_tiktok_flags(tmp_path: Path) -> None:
    import time as _t
    from kintaro_auto.shadow_checker import inspect_aweme_shadow_signals, run_batch_shadowban_check

    now = _t.time()
    clean_item = {
        "aweme_id": "77120001",
        "aweme_type": 150,
        "desc": "#fyp #piratetoken",
        "region": "US",
        "create_time": int(now - 3600),
        "is_nff_or_nr": False,
        "statistics": {"play_count": 145, "digg_count": 12, "comment_count": 1, "share_count": 2, "collect_count": 3},
        "status": {"is_delete": False, "is_prohibited": False, "in_reviewing": False, "reviewed": 1, "self_see": False, "private_status": 0, "allow_share": True, "review_result": {"review_status": 0}, "violation_display_status": 0},
        "risk_infos": {"risk_sink": False, "warn": False, "type": 0},
    }
    shadow_item = {
        "aweme_id": "77120002",
        "aweme_type": 150,
        "desc": "#fyp",
        "region": "US",
        "create_time": int(now - 3600),
        "is_nff_or_nr": True,
        "statistics": {"play_count": 0, "digg_count": 0, "comment_count": 0, "share_count": 0, "collect_count": 0},
        "status": {"is_delete": False, "is_prohibited": False, "in_reviewing": False, "reviewed": 1, "self_see": False, "private_status": 0, "allow_share": True, "review_result": {"review_status": 0}, "violation_display_status": 0},
        "risk_infos": {"risk_sink": True, "warn": False, "type": 0},
    }

    r_clean = inspect_aweme_shadow_signals(
        clean_item,
        username="clean_u",
        studio_flagged=True,
        studio_penalty_info={"reason_labels": ["UNORIGINAL_NR"], "appeal_status_label": "CAN_SUBMIT", "can_submit": True},
        now_ts=now,
    )
    # A live post with 145 views and clean public flags MUST stay "Тень: Нет" even if Studio has UNORIGINAL_NR note!
    assert r_clean["shadow_code"] == "no"
    assert r_clean["shadow_label"] == "Нет"
    assert "UNORIGINAL_NR" in r_clean["studio_note"]
    assert r_clean["studio_can_submit"] is True
    assert r_clean["moderation_label"] == "Пройдена"
    assert r_clean["region_flag"] == "🇺🇸"

    # Also verify anti_unoriginal preset + stratified copy_index produces distinct renders
    from kintaro_auto.uniquifier import uniquify_photo_bytes

    src_buf = tmp_path / "src_anti.jpg"
    Image.new("RGB", (400, 700), (60, 120, 200)).save(src_buf, format="JPEG")
    raw_b = src_buf.read_bytes()
    b0 = uniquify_photo_bytes(raw_b, mode="anti_unoriginal", copy_index=0, rng=random.Random(101))
    b1 = uniquify_photo_bytes(raw_b, mode="anti_unoriginal", copy_index=1, rng=random.Random(101))
    assert b0 != b1 and len(b0) > 1000 and len(b1) > 1000

    r_shadow = inspect_aweme_shadow_signals(shadow_item, username="bad_u", now_ts=now)
    assert r_shadow["shadow_code"] == "yes"
    assert r_shadow["shadow_label"] == "Да"
    assert any("is_nff_or_nr" in x for x in r_shadow["reasons"])
    assert any("risk_sink" in x for x in r_shadow["reasons"])

    def kt_handler(req: httpx.Request) -> httpx.Response:
        u = req.url.params.get("username", "")
        if req.url.path == "/v1/user/get":
            return httpx.Response(200, json={"user": {"unique_id": u, "follower_count": 50, "aweme_count": 1}})
        if req.url.path == "/v1/user/posts":
            return httpx.Response(200, json={"aweme_list": [clean_item] if u == "clean_u" else [shadow_item]})
        return httpx.Response(200, json={})

    cfg = AppConfig(state_dir=str(tmp_path / "state"))
    cfg.api.api_key = "demo"
    cfg.api.save_raw_responses = False
    store = StateStore(cfg.state_dir)
    store.save_account(
        AccountRecord(
            account_id="probe_1",
            username="probe_1",
            proxy="socks5://u:p@127.0.0.1:1080",
            region="US",
            authenticated=True,
            device={"cookies": {"sessionid": "s1"}},
        )
    )

    async with KintaroClient(cfg, transport=httpx.MockTransport(kt_handler)) as client:
        batch = await run_batch_shadowban_check(
            client,
            store,
            usernames=["@clean_u", "https://www.tiktok.com/@bad_u"],
            threads=4,
            deep_audit=False,
        )

    assert batch["counts"]["total"] == 2
    assert batch["counts"]["clean"] == 1
    assert batch["counts"]["shadowbanned"] == 1
    assert "🌟 Тень: Нет" in batch["results"][0]["telegram_text"]
    assert "🌟 Тень: Да" in batch["results"][1]["telegram_text"]


def test_web_watchdog_autostart_and_external_cache_reload(tmp_path: Path) -> None:
    import time
    from fastapi.testclient import TestClient
    from kintaro_auto.web import create_web_app

    cfg_path = tmp_path / "config.toml"
    sdir = tmp_path / "state"
    cfg_path.write_text(f'state_dir = "{sdir}"\n', encoding="utf-8")

    # 1. Default auto_start_watchdog=False keeps watchdog OFF in unit tests
    app_off = create_web_app(cfg_path)
    with TestClient(app_off) as tc:
        st = tc.get("/api/state").json()
        assert st["watchdog"]["enabled"] is False
        assert st["watchdog"]["threads"] == 5

        # External audit_cache.json write is picked up live by reload_env()
        store = StateStore(sdir)
        store.save_account(
            AccountRecord(
                account_id="ext_u1",
                username="ext_u1",
                proxy="socks5://u:p@127.0.0.1:1080",
                region="US",
                authenticated=True,
                device={"cookies": {"sessionid": "s1"}},
            )
        )
        cache_file = sdir / "audit_cache.json"
        time.sleep(0.02)
        cache_file.write_text(
            json.dumps({"ext_u1": {"account_health": "eligible", "audited_at": "04:55:00", "total_posts": 6, "total_views": 320}}),
            encoding="utf-8",
        )
        st2 = tc.get("/api/state").json()
        assert len(st2["accounts"]) == 1
        assert st2["accounts"][0]["audit"]["account_health"] == "eligible"
        assert st2["accounts"][0]["audit"]["audited_at"] == "04:55:00"

    # 2. auto_start_watchdog=True starts 5-thread watchdog via FastAPI lifespan & stops cleanly on exit
    sdir2 = tmp_path / "state_on"
    cfg_path2 = tmp_path / "config2.toml"
    cfg_path2.write_text(f'state_dir = "{sdir2}"\n', encoding="utf-8")
    app_on = create_web_app(cfg_path2, auto_start_watchdog=True)
    with TestClient(app_on) as tc2:
        time.sleep(0.05)
        st_on = tc2.get("/api/state").json()
        assert st_on["watchdog"]["enabled"] is True
        assert st_on["watchdog"]["threads"] == 5
        assert st_on["watchdog"]["cycles"] >= 1
        r_stop = tc2.post("/api/watchdog", json={"enabled": False})
        assert r_stop.status_code == 200 and r_stop.json()["enabled"] is False


@pytest.mark.asyncio
async def test_goal_autopilot_600_reaches_target_smart_topup_and_repour(tmp_path: Path) -> None:
    import asyncio
    from kintaro_auto.appeals import AccountAppealSummary, AccountAuditResult
    from kintaro_auto.autopilot import GoalAutopilotConfig, GoalAutopilotManager
    from kintaro_auto.models import CampaignAccountResult

    sdir = tmp_path / "state"
    mdir = sdir / "media"
    mdir.mkdir(parents=True, exist_ok=True)
    slide = mdir / "creo1.jpg"
    Image.new("RGB", (200, 300), (80, 120, 95)).save(slide, format="JPEG")

    cfg = AppConfig(state_dir=str(sdir))
    store = StateStore(sdir)
    for aid in ("ap_fast", "ap_topup", "ap_repour"):
        store.save_account(
            AccountRecord(
                account_id=aid,
                username=aid,
                password="p",
                proxy="socks5://u:p@127.0.0.1:1080",
                region="US",
                authenticated=True,
                device={"cookies": {"sessionid": "s1"}},
            )
        )

    audit_cache: dict[str, dict] = {
        aid: {"account_health": "eligible", "total_posts": 0, "total_views": 0}
        for aid in ("ap_fast", "ap_topup", "ap_repour")
    }
    busy_set: set[str] = set()
    managed_set: set[str] = set()
    recorded_campaigns: dict[str, list[tuple[int, bool, str]]] = {
        "ap_fast": [],
        "ap_topup": [],
        "ap_repour": [],
    }
    mon_calls: dict[str, int] = {"ap_fast": 0, "ap_topup": 0, "ap_repour": 0}

    async def _fake_upload(acc: AccountRecord, camp: CampaignConfig, _cb) -> CampaignAccountResult:
        recorded_campaigns[acc.account_id].append((camp.copies_count, camp.delete_old_posts, camp.photo_mode))
        return CampaignAccountResult(
            account_id=acc.account_id,
            username=acc.username or acc.account_id,
            status="completed",
            posts_published=camp.copies_count,
            target_posts=camp.copies_count,
            appeals_sent=2,
            initial_views=20,
            post_urls=[f"https://www.tiktok.com/@{acc.account_id}/photo/100{len(recorded_campaigns[acc.account_id])}"],
        )

    async def _fake_appeal(acc: AccountRecord, _gcfg: GoalAutopilotConfig, _stop) -> AccountAppealSummary:
        return AccountAppealSummary(
            account_id=acc.account_id,
            audit=AccountAuditResult(account_id=acc.account_id, username=acc.account_id, account_health="eligible"),
            notice_appeals_verified=1,
        )

    async def _fake_monitor(acc: AccountRecord, _gcfg: GoalAutopilotConfig) -> dict:
        aid = acc.account_id
        mon_calls[aid] += 1
        rnum = len(recorded_campaigns[aid])
        if aid == "ap_fast":
            return {"views_total": 640, "best_post_views": 220, "posts_live": 6, "appeals_sent": 0, "health": "eligible", "folder_key": "clean", "username": aid}
        if aid == "ap_topup":
            if rnum == 1:
                return {"views_total": 260, "best_post_views": 90, "posts_live": 6, "appeals_sent": 0, "health": "eligible", "folder_key": "clean", "username": aid}
            return {"views_total": 680, "best_post_views": 240, "posts_live": 9, "appeals_sent": 1, "health": "eligible", "folder_key": "clean", "username": aid}
        # ap_repour: dead round 1 (<150), hits 620 on round 2!
        if rnum == 1:
            return {"views_total": 20, "best_post_views": 10, "posts_live": 6, "appeals_sent": 0, "health": "eligible", "folder_key": "clean", "username": aid}
        return {"views_total": 620, "best_post_views": 210, "posts_live": 6, "appeals_sent": 0, "health": "eligible", "folder_key": "clean", "username": aid}

    async def _fast_sleep(_s: float) -> None:
        await asyncio.sleep(0.001)

    mgr = GoalAutopilotManager(
        get_cfg=lambda: cfg,
        get_store=lambda: store,
        get_audit_cache=lambda: audit_cache,
        busy_upload_accounts=busy_set,
        managed_accounts=managed_set,
        upload_round_fn=_fake_upload,
        appeal_fn=_fake_appeal,
        monitor_fn=_fake_monitor,
        sleep_fn=_fast_sleep,
    )

    # Override time.time slightly via short wait window (`reupload_wait_minutes=0` clamped to 2 -> monkeypatch time)
    orig_start = mgr.start
    gcfg = GoalAutopilotConfig(
        target_views=600,
        media_files=["creo1.jpg"],
        copies_per_round=6,
        topup_copies=3,
        protect_views_threshold=150,
        max_rounds=3,
        threads=3,
        appeal_threads=3,
        monitor_interval_sec=15,
        reupload_wait_minutes=2,
        scope="clean_empty",
        auto_refill_pool=False,
    )

    # Advance clock across monitor sleeps so wait_limit_min is reached immediately in test
    fake_now = [1_000_000.0]
    import kintaro_auto.autopilot as ap_mod
    orig_time = ap_mod.time.time

    async def _clock_sleep(s: float) -> None:
        fake_now[0] += max(s, 150.0)
        await asyncio.sleep(0.002)

    mgr._sleep_fn = _clock_sleep
    ap_mod.time.time = lambda: fake_now[0]
    try:
        mgr.start(gcfg)
        for _ in range(200):
            if not mgr.running:
                break
            await asyncio.sleep(0.01)
    finally:
        ap_mod.time.time = orig_time

    snap = mgr.snapshot()
    assert snap["running"] is False
    assert snap["counts"]["goal_reached"] == 3
    assert snap["counts"]["total_views"] == 640 + 680 + 620

    # Verify smart strategies!
    # ap_fast: 1 round (6 posts, delete_old=False, anti_unoriginal)
    assert recorded_campaigns["ap_fast"] == [(6, False, "anti_unoriginal")]
    # ap_topup: round 1 (6, False, anti_unoriginal) -> round 2 NON-DESTRUCTIVE TOP-UP (3, False, minimal_snow)!
    assert recorded_campaigns["ap_topup"] == [(6, False, "anti_unoriginal"), (3, False, "minimal_snow")]
    # ap_repour: round 1 (6, False, anti_unoriginal) -> round 2 CLEAN RE-POUR (6, True, minimal_snow)!
    assert recorded_campaigns["ap_repour"] == [(6, False, "anti_unoriginal"), (6, True, "minimal_snow")]

    # Verify copy export & persisted state
    cp = mgr.build_copy_payload(scope="reached", fmt="usernames")
    assert cp["count"] == 3
    assert "@ap_fast" in cp["text"] and "@ap_topup" in cp["text"] and "@ap_repour" in cp["text"]
    assert (sdir / "autopilot_state.json").is_file()
    assert not busy_set and not managed_set


def test_web_goal_autopilot_api_endpoints(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient
    from kintaro_auto.web import create_web_app

    cfg_path = tmp_path / "config.toml"
    sdir = tmp_path / "state"
    cfg_path.write_text(f'state_dir = "{sdir}"\n', encoding="utf-8")
    app = create_web_app(cfg_path)
    with TestClient(app) as tc:
        st = tc.get("/api/state").json()
        assert "goal_autopilot" in st
        assert st["goal_autopilot"]["running"] is False
        r_st = tc.get("/api/autopilot/goal/status")
        assert r_st.status_code == 200 and r_st.json()["ok"] is True
        r_cp = tc.get("/api/autopilot/goal/copy?scope=reached&fmt=lines")
        assert r_cp.status_code == 200 and r_cp.json()["count"] == 0

        # Saved Autopilot configuration & built-in strategy presets
        r_cfg = tc.get("/api/autopilot/goal/config")
        assert r_cfg.status_code == 200
        body_cfg = r_cfg.json()
        assert body_cfg["config"]["target_views"] == 600
        preset_ids = {p["id"] for p in body_cfg["presets"]}
        assert {"standard_600", "turbo_600", "night_safe_600", "viral_post_1000"} <= preset_ids

        r_save = tc.post(
            "/api/autopilot/goal/config",
            json={
                "target_views": 850,
                "target_mode": "max_post",
                "threads": 4,
                "appeal_threads": 4,
                "copies_per_round": 6,
                "topup_copies": 2,
                "reupload_wait_minutes": 30,
                "max_patience_minutes": 60,
                "delay_min_sec": 12.0,
                "delay_max_sec": 18.0,
                "description": "#custom #fyp",
                "auto_music_query": "Phonk",
            },
        )
        assert r_save.status_code == 200
        assert r_save.json()["config"]["target_views"] == 850
        assert r_save.json()["config"]["target_mode"] == "max_post"
        assert (sdir / "autopilot_config.json").is_file()

    # Verify saved config survives server restart!
    app2 = create_web_app(cfg_path)
    with TestClient(app2) as tc2:
        reloaded = tc2.get("/api/autopilot/goal/config").json()["config"]
        assert reloaded["target_views"] == 850
        assert reloaded["target_mode"] == "max_post"
        assert reloaded["threads"] == 4
        assert reloaded["auto_music_query"] == "Phonk"

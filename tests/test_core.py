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

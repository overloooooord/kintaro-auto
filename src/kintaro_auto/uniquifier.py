from __future__ import annotations

import functools
import hashlib
import json
import random
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from PIL import Image, ImageEnhance, ImageOps

from kintaro_auto.config import UniquifierSettings

VIDEO_MAX_BYTES = 50 * 1024 * 1024   # 50 MB per kintaro-api.json
PHOTO_MAX_BYTES = 10 * 1024 * 1024   # 10 MB per kintaro-api.json
PHOTO_MAX_COUNT = 10                 # Max 10 files per kintaro-api.json


@dataclass
class PreparedMedia:
    path: Path
    mime_type: str
    sha256: str
    size_bytes: int
    is_temporary: bool


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@functools.lru_cache(maxsize=4)
def detect_h264_encoder(ffmpeg_bin: str = "ffmpeg") -> str:
    """Detect available H.264 encoder (`libx264` or Fedora/free `libopenh264`)."""
    proc = subprocess.run(
        [ffmpeg_bin, "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        check=False,
    )
    out = proc.stdout or ""
    for enc in ("libx264", "libopenh264", "h264_vaapi", "mpeg4"):
        if f" {enc} " in out:
            return enc
    raise RuntimeError(
        f"В `{ffmpeg_bin}` не найден ни `libx264`, ни `libopenh264` для кодирования MP4."
    )


def probe_video(
    path: Path, ffprobe_bin: str = "ffprobe"
) -> tuple[int, int, bool, int]:
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,width,height,bit_rate:format=bit_rate",
        "-of",
        "json",
        str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(
            f"Ошибка ffprobe при чтении {path}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    info = json.loads(proc.stdout or "{}")
    streams = info.get("streams", [])
    width, height = 0, 0
    has_audio = False
    bitrate_bps = 0
    for s in streams:
        ctype = s.get("codec_type")
        if ctype == "video" and width == 0:
            width = int(s.get("width") or 0)
            height = int(s.get("height") or 0)
            if str(s.get("bit_rate", "")).isdigit():
                bitrate_bps = int(s["bit_rate"])
        elif ctype == "audio":
            has_audio = True
    if bitrate_bps <= 0:
        fmt_br = (info.get("format") or {}).get("bit_rate")
        if str(fmt_br or "").isdigit():
            bitrate_bps = int(fmt_br)
    if width <= 0 or height <= 0:
        raise RuntimeError(f"В файле {path} не найден видеопоток с корректным разрешением.")
    return width, height, has_audio, bitrate_bps


def uniquify_video(
    source_path: Path,
    settings: UniquifierSettings,
    rng: random.Random | None = None,
) -> PreparedMedia:
    if not source_path.exists():
        raise FileNotFoundError(f"Видео не найдено: {source_path}")

    if not settings.enabled:
        size = source_path.stat().st_size
        if size > VIDEO_MAX_BYTES:
            raise ValueError(
                f"Видео {source_path} весит {size / (1024 * 1024):.2f} МБ (лимит Kintaro API — 50 МБ)."
            )
        return PreparedMedia(
            path=source_path,
            mime_type="video/mp4",
            sha256=_sha256_file(source_path),
            size_bytes=size,
            is_temporary=False,
        )

    r = rng or random.Random()
    width, height, has_audio, src_bitrate = probe_video(
        source_path, settings.ffprobe_bin
    )
    target_w = max(2, width - (width % 2))
    target_h = max(2, height - (height % 2))

    crop_ratio = r.uniform(settings.crop_ratio_min, settings.crop_ratio_max)
    brightness = r.uniform(-settings.brightness_shift_max, settings.brightness_shift_max)
    contrast = 1.0 + r.uniform(-settings.contrast_shift_max, settings.contrast_shift_max)
    saturation = 1.0 + r.uniform(-settings.saturation_shift_max, settings.saturation_shift_max)
    speed = 1.0 + r.uniform(-settings.speed_jitter_max, settings.speed_jitter_max)
    crf = r.randint(settings.video_crf_min, settings.video_crf_max)

    out_dir = Path(settings.temp_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"vid_{uuid.uuid4().hex[:12]}.mp4"

    vf = (
        f"crop=iw*{crop_ratio:.5f}:ih*{crop_ratio:.5f},"
        f"scale={target_w}:{target_h}:flags=lanczos,"
        f"eq=brightness={brightness:.5f}:contrast={contrast:.5f}:saturation={saturation:.5f},"
        f"setpts={(1.0 / speed):.6f}*PTS"
    )

    encoder = detect_h264_encoder(settings.ffmpeg_bin)
    if encoder == "libx264":
        vcodec_args = [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            str(crf),
            "-pix_fmt",
            "yuv420p",
        ]
    elif encoder == "libopenh264":
        # Compute adaptive bitrate from resolution or source bitrate + +-9% random jitter
        base_kbps = (
            max(600, min(8500, src_bitrate // 1000))
            if src_bitrate > 0
            else max(800, int((target_w * target_h) * 0.0018))
        )
        jittered_kbps = int(base_kbps * r.uniform(0.91, 1.09))
        vcodec_args = [
            "-c:v",
            "libopenh264",
            "-b:v",
            f"{jittered_kbps}k",
            "-pix_fmt",
            "yuv420p",
        ]
    else:
        vcodec_args = ["-c:v", encoder, "-pix_fmt", "yuv420p"]

    cmd: list[str] = [
        settings.ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source_path),
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-fflags",
        "+bitexact",
        "-flags:v",
        "+bitexact",
        "-flags:a",
        "+bitexact",
        "-brand",
        "mp42",
        "-metadata:s:v:0",
        "handler_name=VideoHandle",
        "-vf",
        vf,
        *vcodec_args,
    ]

    if has_audio:
        audio_kbps = r.choice([124, 128, 132, 136])
        cmd.extend(
            [
                "-metadata:s:a:0",
                "handler_name=SoundHandle",
                "-af",
                f"atempo={speed:.5f}",
                "-c:a",
                "aac",
                "-b:a",
                f"{audio_kbps}k",
            ]
        )
    else:
        cmd.append("-an")

    cmd.extend(
        [
            "-movflags",
            "+faststart",
            str(out_path),
        ]
    )

    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not out_path.exists():
        raise RuntimeError(
            f"Ошибка уникализации видео через ffmpeg ({encoder}): {proc.stderr.strip() or proc.stdout.strip()}"
        )

    size = out_path.stat().st_size
    if size > VIDEO_MAX_BYTES:
        out_path.unlink(missing_ok=True)
        raise ValueError(
            f"Уникализированное видео превысило лимит 50 МБ ({size / (1024 * 1024):.2f} МБ). Уменьши длительность/битрейт."
        )

    return PreparedMedia(
        path=out_path,
        mime_type="video/mp4",
        sha256=_sha256_file(out_path),
        size_bytes=size,
        is_temporary=True,
    )


def uniquify_photos(
    source_paths: list[Path],
    settings: UniquifierSettings,
    rng: random.Random | None = None,
) -> list[PreparedMedia]:
    if not source_paths:
        raise ValueError("Не передано ни одного фото для фото-поста.")
    if len(source_paths) > PHOTO_MAX_COUNT:
        raise ValueError(
            f"Kintaro API поддерживает максимум {PHOTO_MAX_COUNT} фото в одном посте (передано {len(source_paths)})."
        )

    r = rng or random.Random()
    prepared: list[PreparedMedia] = []
    out_dir = Path(settings.temp_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    for src in source_paths:
        if not src.exists():
            raise FileNotFoundError(f"Фото не найдено: {src}")

        if not settings.enabled:
            ext = src.suffix.lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            size = src.stat().st_size
            if size > PHOTO_MAX_BYTES:
                raise ValueError(f"Фото {src} превышает лимит 10 МБ.")
            prepared.append(
                PreparedMedia(
                    path=src,
                    mime_type=mime,
                    sha256=_sha256_file(src),
                    size_bytes=size,
                    is_temporary=False,
                )
            )
            continue

        with Image.open(src) as raw_img:
            img = ImageOps.exif_transpose(raw_img)
            has_real_alpha = (
                img.mode in ("RGBA", "LA")
                and img.getchannel("A").getextrema() != (255, 255)
            )
            img = img.convert("RGBA" if has_real_alpha else "RGB")
            orig_w, orig_h = img.size

            # Micro-rotation (+-0.15..0.40 deg) + inner crop breaks DCT/pHash collisions cleanly
            angle = r.choice([-1.0, 1.0]) * r.uniform(0.15, 0.40)
            img = img.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False)

            crop_ratio = r.uniform(settings.crop_ratio_min, settings.crop_ratio_max)
            crop_w = max(2, int(orig_w * crop_ratio))
            crop_h = max(2, int(orig_h * crop_ratio))
            left = r.randint(0, max(0, orig_w - crop_w))
            top = r.randint(0, max(0, orig_h - crop_h))

            img = img.crop((left, top, left + crop_w, top + crop_h)).resize(
                (orig_w, orig_h), Image.Resampling.LANCZOS
            )

            if not has_real_alpha:
                img = ImageEnhance.Brightness(img).enhance(
                    1.0 + r.uniform(-settings.brightness_shift_max, settings.brightness_shift_max)
                )
                img = ImageEnhance.Contrast(img).enhance(
                    1.0 + r.uniform(-settings.contrast_shift_max, settings.contrast_shift_max)
                )
                img = ImageEnhance.Color(img).enhance(
                    1.0 + r.uniform(-settings.saturation_shift_max, settings.saturation_shift_max)
                )

            # Mutate scattered 8x8 DCT block centers by +-3 so even solid-color slides vary after JPEG quantization
            px = img.load()
            if px is not None:
                for _ in range(32):
                    rx = r.randrange(orig_w)
                    ry = r.randrange(orig_h)
                    pixel = list(px[rx, ry])
                    ch = r.randrange(3)
                    delta = r.choice([-3, -2, 2, 3])
                    pixel[ch] = max(0, min(255, pixel[ch] + delta))
                    px[rx, ry] = tuple(pixel)

            if has_real_alpha:
                out_path = out_dir / f"img_{uuid.uuid4().hex[:12]}.png"
                img.save(out_path, format="PNG", optimize=True)
                mime = "image/png"
            else:
                out_path = out_dir / f"img_{uuid.uuid4().hex[:12]}.jpg"
                quality = r.randint(
                    settings.photo_jpeg_quality_min, settings.photo_jpeg_quality_max
                )
                img.save(out_path, format="JPEG", quality=quality, subsampling=0)
                mime = "image/jpeg"

        size = out_path.stat().st_size
        if size > PHOTO_MAX_BYTES:
            out_path.unlink(missing_ok=True)
            raise ValueError(
                f"Уникализированное фото {src} превысило лимит 10 МБ ({size / (1024 * 1024):.2f} МБ)."
            )

        prepared.append(
            PreparedMedia(
                path=out_path,
                mime_type=mime,
                sha256=_sha256_file(out_path),
                size_bytes=size,
                is_temporary=True,
            )
        )

    return prepared

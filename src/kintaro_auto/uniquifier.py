from __future__ import annotations

import colorsys
import functools
import hashlib
import io
import json
import math
import random
import shutil
import struct
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps

from kintaro_auto.config import UniquifierSettings

VIDEO_MAX_BYTES = 50 * 1024 * 1024   # 50 MB per kintaro-api.json
PHOTO_MAX_BYTES = 10 * 1024 * 1024   # 10 MB per kintaro-api.json
PHOTO_MAX_COUNT = 10                 # Max 10 files per kintaro-api.json


PHOTO_UNIQUIFIER_PRESETS: dict[str, dict[str, Any]] = {
    "minimal_snow": {
        "mode": "minimal_snow",
        "crop_x_min_pct": 0.8,
        "crop_x_max_pct": 1.5,
        "crop_y_min_pct": 1.2,
        "crop_y_max_pct": 2.2,
        "max_tilt_deg": 0.25,
        "snow_min_flakes": 115,
        "snow_max_flakes": 175,
        "enable_bokeh": False,
        "enable_wind_trail": False,
        "tone_jitter_pct": 1.2,
        "noise_sigma": 1.2,
        "vignette_pct": 0.0,
        "border_px": 0,
        "flip_horizontal": False,
        "perspective_pct": 0.0,
        "ambient_frame_pct": 0.0,
        "hue_shift_deg": 0.0,
        "light_leak_pct": 0.0,
        "chromatic_px": 0,
        "canvas_jitter_px": 0,
        "jpeg_quality_min": 90,
        "jpeg_quality_max": 95,
    },
    "minimal": {
        "mode": "minimal",
        "crop_x_min_pct": 0.8,
        "crop_x_max_pct": 1.5,
        "crop_y_min_pct": 1.2,
        "crop_y_max_pct": 2.2,
        "max_tilt_deg": 0.25,
        "snow_min_flakes": 45,
        "snow_max_flakes": 85,
        "enable_bokeh": False,
        "enable_wind_trail": False,
        "tone_jitter_pct": 1.2,
        "noise_sigma": 1.2,
        "vignette_pct": 0.0,
        "border_px": 0,
        "flip_horizontal": False,
        "perspective_pct": 0.0,
        "ambient_frame_pct": 0.0,
        "hue_shift_deg": 0.0,
        "light_leak_pct": 0.0,
        "chromatic_px": 0,
        "canvas_jitter_px": 0,
        "jpeg_quality_min": 90,
        "jpeg_quality_max": 95,
    },
    "stealth": {
        "mode": "stealth",
        "crop_x_min_pct": 1.2,
        "crop_x_max_pct": 2.4,
        "crop_y_min_pct": 1.6,
        "crop_y_max_pct": 3.0,
        "max_tilt_deg": 0.45,
        "snow_min_flakes": 0,
        "snow_max_flakes": 0,
        "enable_bokeh": False,
        "enable_wind_trail": False,
        "tone_jitter_pct": 2.2,
        "noise_sigma": 2.4,
        "vignette_pct": 4.0,
        "border_px": 0,
        "flip_horizontal": False,
        "perspective_pct": 1.2,
        "ambient_frame_pct": 0.0,
        "hue_shift_deg": 4.0,
        "light_leak_pct": 5.0,
        "chromatic_px": 1,
        "canvas_jitter_px": 8,
        "jpeg_quality_min": 89,
        "jpeg_quality_max": 95,
    },
    "medium": {
        "mode": "medium",
        "crop_x_min_pct": 1.6,
        "crop_x_max_pct": 2.8,
        "crop_y_min_pct": 2.4,
        "crop_y_max_pct": 4.2,
        "max_tilt_deg": 0.55,
        "snow_min_flakes": 140,
        "snow_max_flakes": 240,
        "enable_bokeh": False,
        "enable_wind_trail": True,
        "tone_jitter_pct": 2.0,
        "noise_sigma": 1.8,
        "vignette_pct": 3.5,
        "border_px": 0,
        "flip_horizontal": False,
        "perspective_pct": 0.8,
        "ambient_frame_pct": 0.0,
        "hue_shift_deg": 3.0,
        "light_leak_pct": 4.0,
        "chromatic_px": 0,
        "canvas_jitter_px": 6,
        "jpeg_quality_min": 89,
        "jpeg_quality_max": 95,
    },
    "boosted": {
        "mode": "boosted",
        "crop_x_min_pct": 2.6,
        "crop_x_max_pct": 4.3,
        "crop_y_min_pct": 4.2,
        "crop_y_max_pct": 7.2,
        "max_tilt_deg": 1.10,
        "snow_min_flakes": 330,
        "snow_max_flakes": 510,
        "enable_bokeh": True,
        "enable_wind_trail": True,
        "tone_jitter_pct": 3.2,
        "noise_sigma": 2.4,
        "vignette_pct": 5.5,
        "border_px": 0,
        "flip_horizontal": False,
        "perspective_pct": 1.6,
        "ambient_frame_pct": 2.5,
        "hue_shift_deg": 7.0,
        "light_leak_pct": 8.0,
        "chromatic_px": 1,
        "canvas_jitter_px": 12,
        "jpeg_quality_min": 87,
        "jpeg_quality_max": 94,
    },
    "ultra_hard": {
        "mode": "ultra_hard",
        "crop_x_min_pct": 0.2,
        "crop_x_max_pct": 0.8,
        "crop_y_min_pct": 0.5,
        "crop_y_max_pct": 1.6,
        "max_tilt_deg": 2.60,
        "snow_min_flakes": 240,
        "snow_max_flakes": 440,
        "enable_bokeh": True,
        "enable_wind_trail": True,
        "tone_jitter_pct": 5.2,
        "noise_sigma": 3.6,
        "vignette_pct": 8.0,
        "border_px": 3,
        "flip_horizontal": False,
        "perspective_pct": 2.4,
        "ambient_frame_pct": 11.5,
        "hue_shift_deg": 22.0,
        "light_leak_pct": 16.5,
        "chromatic_px": 2,
        "canvas_jitter_px": 28,
        "jpeg_quality_min": 83,
        "jpeg_quality_max": 92,
    },
}


@dataclass
class PreparedMedia:
    path: Path
    mime_type: str
    sha256: str
    size_bytes: int
    is_temporary: bool


@dataclass
class PhotoProfile:
    mode: str = "minimal_snow"
    crop_x_min_pct: float = 0.8
    crop_x_max_pct: float = 1.5
    crop_y_min_pct: float = 1.2
    crop_y_max_pct: float = 2.2
    max_tilt_deg: float = 0.25
    snow_min_flakes: int = 115
    snow_max_flakes: int = 175
    enable_bokeh: bool = False
    enable_wind_trail: bool = False
    tone_jitter_pct: float = 1.2
    noise_sigma: float = 1.2
    vignette_pct: float = 0.0
    border_px: int = 0
    flip_horizontal: bool = False
    perspective_pct: float = 0.0
    ambient_frame_pct: float = 0.0
    hue_shift_deg: float = 0.0
    light_leak_pct: float = 0.0
    chromatic_px: int = 0
    canvas_jitter_px: int = 0
    jpeg_quality_min: int = 90
    jpeg_quality_max: int = 95

    @classmethod
    def resolve(
        cls,
        settings: UniquifierSettings | None = None,
        *,
        mode_override: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> PhotoProfile:
        raw_mode = (
            mode_override
            or (overrides or {}).get("mode")
            or (settings.photo_mode if settings else "minimal_snow")
            or "minimal_snow"
        ).strip().lower()
        aliases = {
            "min": "minimal",
            "light": "minimal",
            "low": "minimal",
            "med": "medium",
            "normal": "medium",
            "heavy": "boosted",
            "strong": "boosted",
            "max": "boosted",
            "ultra": "ultra_hard",
            "hard": "ultra_hard",
            "hardcore": "ultra_hard",
            "extreme": "ultra_hard",
        }
        raw_mode = aliases.get(raw_mode, raw_mode)
        if raw_mode == "off":
            return cls(mode="off")

        base = dict(PHOTO_UNIQUIFIER_PRESETS.get(raw_mode, PHOTO_UNIQUIFIER_PRESETS["minimal_snow"]))
        base["mode"] = raw_mode

        if settings is not None:
            for k in (
                "crop_x_min_pct", "crop_x_max_pct", "crop_y_min_pct", "crop_y_max_pct",
                "max_tilt_deg", "snow_min_flakes", "snow_max_flakes", "enable_bokeh",
                "enable_wind_trail", "tone_jitter_pct", "noise_sigma", "vignette_pct",
                "border_px", "flip_horizontal", "perspective_pct", "ambient_frame_pct",
                "hue_shift_deg", "light_leak_pct", "chromatic_px", "canvas_jitter_px",
            ):
                val = getattr(settings, k, None)
                if val is not None:
                    base[k] = val
            if raw_mode == "custom":
                base["jpeg_quality_min"] = settings.photo_jpeg_quality_min
                base["jpeg_quality_max"] = settings.photo_jpeg_quality_max

        if overrides:
            for k, v in overrides.items():
                if k in base and v is not None and v != "":
                    base[k] = v

        cx1 = max(0.0, min(14.0, float(base["crop_x_min_pct"])))
        cx2 = max(cx1, min(16.0, float(base["crop_x_max_pct"])))
        cy1 = max(0.0, min(16.0, float(base["crop_y_min_pct"])))
        cy2 = max(cy1, min(20.0, float(base["crop_y_max_pct"])))
        q1 = max(70, min(99, int(float(base["jpeg_quality_min"]))))
        q2 = max(q1, min(99, int(float(base["jpeg_quality_max"]))))
        s1 = max(0, min(1500, int(float(base["snow_min_flakes"]))))
        s2 = max(s1, min(2000, int(float(base["snow_max_flakes"]))))

        return cls(
            mode=raw_mode,
            crop_x_min_pct=cx1,
            crop_x_max_pct=cx2,
            crop_y_min_pct=cy1,
            crop_y_max_pct=cy2,
            max_tilt_deg=max(0.0, min(5.0, float(base["max_tilt_deg"]))),
            snow_min_flakes=s1,
            snow_max_flakes=s2,
            enable_bokeh=bool(base["enable_bokeh"]),
            enable_wind_trail=bool(base["enable_wind_trail"]),
            tone_jitter_pct=max(0.0, min(12.0, float(base["tone_jitter_pct"]))),
            noise_sigma=max(0.0, min(10.0, float(base["noise_sigma"]))),
            vignette_pct=max(0.0, min(30.0, float(base["vignette_pct"]))),
            border_px=max(0, min(24, int(float(base["border_px"])))),
            flip_horizontal=bool(base["flip_horizontal"]),
            perspective_pct=max(0.0, min(6.0, float(base["perspective_pct"]))),
            ambient_frame_pct=max(0.0, min(16.0, float(base["ambient_frame_pct"]))),
            hue_shift_deg=max(0.0, min(45.0, float(base["hue_shift_deg"]))),
            light_leak_pct=max(0.0, min(28.0, float(base["light_leak_pct"]))),
            chromatic_px=max(0, min(5, int(float(base["chromatic_px"])))),
            canvas_jitter_px=max(0, min(64, int(float(base["canvas_jitter_px"])))),
            jpeg_quality_min=q1,
            jpeg_quality_max=q2,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ── Pure-Python Photo Anti-pHash / Anti-ViT Engine ───────────────────────────


def _solve_perspective_coeffs(
    src_pts: list[tuple[float, float]],
    dst_pts: list[tuple[float, float]],
) -> tuple[float, ...]:
    matrix: list[list[float]] = []
    rhs: list[float] = []
    for (sx, sy), (dx, dy) in zip(src_pts, dst_pts):
        matrix.append([dx, dy, 1.0, 0.0, 0.0, 0.0, -sx * dx, -sx * dy])
        rhs.append(sx)
        matrix.append([0.0, 0.0, 0.0, dx, dy, 1.0, -sy * dx, -sy * dy])
        rhs.append(sy)

    n = 8
    aug = [matrix[i] + [rhs[i]] for i in range(n)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(aug[r][col]))
        aug[col], aug[piv] = aug[piv], aug[col]
        denom = aug[col][col]
        if abs(denom) < 1e-12:
            return (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)
        for j in range(col, n + 1):
            aug[col][j] /= denom
        for row in range(n):
            if row != col:
                factor = aug[row][col]
                if factor != 0.0:
                    for j in range(col, n + 1):
                        aug[row][j] -= factor * aug[col][j]
    return tuple(aug[i][n] for i in range(n))


def _apply_perspective_warp(im: Image.Image, max_pct: float, r: random.Random) -> Image.Image:
    w, h = im.size
    mp = max_pct / 100.0
    dx, dy = w * mp, h * mp
    dst = [(0.0, 0.0), (float(w), 0.0), (float(w), float(h)), (0.0, float(h))]
    ix, iy = dx * 0.95, dy * 0.95
    src_in = [
        (ix + r.uniform(-dx * 0.65, dx * 0.65), iy + r.uniform(-dy * 0.65, dy * 0.65)),
        (w - ix + r.uniform(-dx * 0.65, dx * 0.65), iy + r.uniform(-dy * 0.65, dy * 0.65)),
        (w - ix + r.uniform(-dx * 0.65, dx * 0.65), h - iy + r.uniform(-dy * 0.65, dy * 0.65)),
        (ix + r.uniform(-dx * 0.65, dx * 0.65), h - iy + r.uniform(-dy * 0.65, dy * 0.65)),
    ]
    coeffs = _solve_perspective_coeffs(src_in, dst)
    return im.transform((w, h), Image.Transform.PERSPECTIVE, coeffs, resample=Image.Resampling.BICUBIC)


def _apply_hue_and_channel_curves(
    im: Image.Image, hue_deg: float, tone_pct: float, r: random.Random
) -> Image.Image:
    if tone_pct > 0.5:
        spread = min(0.11, tone_pct * 0.016)
        luts: list[int] = []
        for _ in range(3):
            gamma = r.uniform(1.0 - spread, 1.0 + spread)
            black_lift = r.randint(0, max(1, int(tone_pct * 1.1)))
            white_clip = 255 - r.randint(0, max(1, int(tone_pct * 0.9)))
            span = max(180, white_clip - black_lift)
            luts.extend(
                max(0, min(255, int(round(black_lift + span * ((v / 255.0) ** gamma)))))
                for v in range(256)
            )
        im = im.point(luts)

    if hue_deg > 0.4:
        rad = math.radians(r.uniform(-hue_deg, hue_deg))
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        m00 = cos_a + (1.0 - cos_a) / 3.0
        m01 = (1.0 - cos_a) / 3.0 - math.sqrt(1.0 / 3.0) * sin_a
        m02 = (1.0 - cos_a) / 3.0 + math.sqrt(1.0 / 3.0) * sin_a
        im = im.convert("RGB", (m00, m01, m02, 0.0, m02, m00, m01, 0.0, m01, m02, m00, 0.0))
    return im


def _trim_solid_black_letterbox(im: Image.Image) -> Image.Image:
    w, h = im.size
    small = im.convert("L").resize((32, 128), Image.Resampling.BOX)
    px = small.load()
    if px is None:
        return im
    row_avgs = [sum(px[x, y] for x in range(32)) / 32.0 for y in range(128)]
    top_idx = 0
    while top_idx < 36 and row_avgs[top_idx] < 7.5:
        top_idx += 1
    bot_idx = 127
    while bot_idx > 92 and row_avgs[bot_idx] < 7.5:
        bot_idx -= 1
    if top_idx >= 5 or bot_idx <= 122:
        t_px = max(0, int((top_idx - 1) * h / 128.0))
        b_px = min(h, int((bot_idx + 2) * h / 128.0))
        if b_px - t_px >= int(h * 0.38):
            return im.crop((0, t_px, w, b_px))
    return im


def _apply_diagonal_light_leak(im: Image.Image, leak_pct: float, r: random.Random) -> Image.Image:
    w, h = im.size
    gw, gh = 16, 24
    grad = Image.new("RGB", (gw, gh))
    px = grad.load()
    if px is None:
        return im
    h1 = r.random()
    h2 = (h1 + r.uniform(0.28, 0.68)) % 1.0
    c1 = tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h1, r.uniform(0.55, 0.88), 1.0))
    c2 = tuple(int(x * 255) for x in colorsys.hsv_to_rgb(h2, r.uniform(0.55, 0.88), 0.95))
    ang = r.uniform(0, math.tau)
    vx, vy = math.cos(ang), math.sin(ang)
    wave_freq = r.uniform(1.8, 3.8)
    wave_phase = r.uniform(0, math.tau)
    for y in range(gh):
        ny = (y / max(1, gh - 1)) - 0.5
        for x in range(gw):
            nx = (x / max(1, gw - 1)) - 0.5
            proj = (nx * vx + ny * vy) + 0.5 + 0.20 * math.sin((nx * vy - ny * vx) * wave_freq + wave_phase)
            t = max(0.0, min(1.0, proj))
            px[x, y] = (
                int(c1[0] * (1.0 - t) + c2[0] * t),
                int(c1[1] * (1.0 - t) + c2[1] * t),
                int(c1[2] * (1.0 - t) + c2[2] * t),
            )
    overlay = grad.resize((w, h), resample=Image.Resampling.BILINEAR)
    alpha = min(0.22, r.uniform(leak_pct * 0.65, leak_pct * 1.15) / 100.0)
    return Image.blend(im, overlay, alpha)


def _apply_ambient_floating_frame(
    im: Image.Image,
    target_w: int,
    target_h: int,
    frame_pct: float,
    tilt_deg: float,
    persp_pct: float,
    r: random.Random,
) -> Image.Image:
    card_src = _trim_solid_black_letterbox(im)
    src_w, src_h = card_src.size
    gw, gh = 96, 168
    bg_small = card_src.resize((gw, gh), resample=Image.Resampling.BILINEAR)
    if r.random() < 0.7:
        bg_small = ImageOps.mirror(bg_small)
    if r.random() < 0.5:
        bg_small = ImageOps.flip(bg_small)
    bg_small = _apply_hue_and_channel_curves(bg_small, hue_deg=110.0, tone_pct=6.0, r=r)
    bg_small = ImageEnhance.Brightness(bg_small).enhance(r.uniform(0.42, 0.78))
    bg_small = ImageEnhance.Color(bg_small).enhance(r.uniform(1.4, 2.2))

    orb_draw = ImageDraw.Draw(bg_small)
    acc_hue = r.random()
    for i in range(r.randint(3, 5)):
        oh = (acc_hue + i * r.uniform(0.18, 0.38)) % 1.0
        ocol = tuple(int(x * 255) for x in colorsys.hsv_to_rgb(oh, r.uniform(0.55, 0.90), r.uniform(0.75, 1.0)))
        ox0, oy0 = r.randint(-20, gw + 10), r.randint(-20, gh + 10)
        orad_x, orad_y = r.randint(18, 48), r.randint(24, 64)
        orb_draw.ellipse((ox0 - orad_x, oy0 - orad_y, ox0 + orad_x, oy0 + orad_y), fill=ocol)

    bg = bg_small.filter(ImageFilter.GaussianBlur(radius=10)).resize((target_w, target_h), resample=Image.Resampling.BILINEAR)

    pad_frac = r.uniform(frame_pct * 0.75, frame_pct * 1.18) / 100.0
    max_cw = target_w * max(0.78, min(0.93, 1.0 - pad_frac * 1.35))
    max_ch = target_h * max(0.78, min(0.93, 1.0 - pad_frac * 1.25))
    fit_scale = min(max_cw / max(1, src_w), max_ch / max(1, src_h))
    cw, ch = max(120, int(round(src_w * fit_scale))), max(120, int(round(src_h * fit_scale)))

    card_rgb = card_src.resize((cw, ch), resample=Image.Resampling.LANCZOS)
    corner_r = max(16, int(min(cw, ch) * r.uniform(0.026, 0.046)))

    mask = Image.new("L", (cw, ch), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=corner_r, fill=255)
    rim_col = tuple(int(x * 255) for x in colorsys.hsv_to_rgb(acc_hue, r.uniform(0.40, 0.85), r.uniform(0.75, 1.0)))
    ImageDraw.Draw(card_rgb).rounded_rectangle((0, 0, cw - 1, ch - 1), radius=corner_r, outline=rim_col, width=r.randint(2, 4))
    card_rgba = card_rgb.copy().convert("RGBA")
    card_rgba.putalpha(mask)

    if persp_pct > 0.1:
        pw_pad = int(cw * (persp_pct / 100.0) * 1.1) + 4
        ph_pad = int(ch * (persp_pct / 100.0) * 1.1) + 4
        padded = Image.new("RGBA", (cw + pw_pad * 2, ch + ph_pad * 2), (0, 0, 0, 0))
        padded.paste(card_rgba, (pw_pad, ph_pad))
        pw, ph = padded.size
        dx, dy = cw * (persp_pct / 100.0) * 0.75, ch * (persp_pct / 100.0) * 0.75
        dst = [(0.0, 0.0), (float(pw), 0.0), (float(pw), float(ph)), (0.0, float(ph))]
        src_pts = [
            (r.uniform(-dx, dx), r.uniform(-dy, dy)),
            (pw + r.uniform(-dx, dx), r.uniform(-dy, dy)),
            (pw + r.uniform(-dx, dx), ph + r.uniform(-dy, dy)),
            (r.uniform(-dx, dx), ph + r.uniform(-dy, dy)),
        ]
        card_rgba = padded.transform(
            (pw, ph), Image.Transform.PERSPECTIVE, _solve_perspective_coeffs(src_pts, dst), resample=Image.Resampling.BICUBIC
        )

    if tilt_deg > 0.05:
        card_rgba = card_rgba.rotate(r.uniform(-tilt_deg, tilt_deg), resample=Image.Resampling.BICUBIC, expand=True)

    rw, rh = card_rgba.size
    if rw > target_w - 16 or rh > target_h - 16:
        sh_scale = min((target_w - 16) / rw, (target_h - 16) / rh)
        rw, rh = max(80, int(round(rw * sh_scale))), max(80, int(round(rh * sh_scale)))
        card_rgba = card_rgba.resize((rw, rh), resample=Image.Resampling.LANCZOS)

    ox = int(round(max(0, target_w - rw) * r.uniform(0.12, 0.88)))
    oy = int(round(max(0, target_h - rh) * r.uniform(0.12, 0.88)))

    sh_small = card_rgba.getchannel("A").resize((max(32, rw // 6), max(32, rh // 6)), Image.Resampling.BILINEAR)
    sh_alpha = sh_small.filter(ImageFilter.GaussianBlur(radius=3)).resize((rw, rh), Image.Resampling.BILINEAR).point(lambda a: int(a * 0.55))
    shadow_layer = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    black_card = Image.new("RGBA", (rw, rh), (0, 0, 0, 255))
    black_card.putalpha(sh_alpha)
    shadow_layer.paste(black_card, (min(target_w - rw, ox + r.randint(-4, 7)), min(target_h - rh, oy + r.randint(7, 15))))

    out_rgba = Image.alpha_composite(bg.convert("RGBA"), shadow_layer)
    card_layer = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    card_layer.paste(card_rgba, (ox, oy))
    return Image.alpha_composite(out_rgba, card_layer).convert("RGB")


def uniquify_photo_bytes(
    raw_bytes: bytes,
    profile: PhotoProfile | None = None,
    *,
    mode: str = "minimal_snow",
    overrides: dict[str, Any] | None = None,
    rng: random.Random | None = None,
) -> bytes:
    """
    Renders freshly uniquified JPEG bytes from raw image bytes using the configured
    Anti-pHash / Anti-ViT preset (`minimal_snow` default, `minimal`, `stealth`, `medium`, `boosted`, `ultra_hard`, `custom`, `off`).
    """
    cfg = profile or PhotoProfile.resolve(None, mode_override=mode, overrides=overrides)
    if cfg.mode == "off":
        return raw_bytes

    r = rng or random.Random()
    with Image.open(io.BytesIO(raw_bytes)) as src_im:
        im = ImageOps.exif_transpose(src_im).convert("RGB")
        orig_w, orig_h = im.size

        if cfg.flip_horizontal:
            im = ImageOps.mirror(im)

        use_floating_card = cfg.ambient_frame_pct > 0.4
        if use_floating_card:
            im = _trim_solid_black_letterbox(im)
        card_w, card_h = im.size

        if not use_floating_card and cfg.perspective_pct > 0.05:
            im = _apply_perspective_warp(im, cfg.perspective_pct, r)

        if not use_floating_card and cfg.max_tilt_deg > 0.001:
            im = im.rotate(r.uniform(-cfg.max_tilt_deg, cfg.max_tilt_deg), resample=Image.Resampling.BICUBIC, expand=False)

        cx_min, cx_max = cfg.crop_x_min_pct / 100.0, cfg.crop_x_max_pct / 100.0
        cy_min, cy_max = cfg.crop_y_min_pct / 100.0, cfg.crop_y_max_pct / 100.0
        if cx_max > 0.0005 or cy_max > 0.0005:
            left = int(card_w * r.uniform(cx_min, cx_max))
            right = card_w - int(card_w * r.uniform(cx_min, cx_max))
            top = int(card_h * r.uniform(cy_min, cy_max))
            bottom = card_h - int(card_h * r.uniform(cy_min, cy_max))
            if right - left > 64 and bottom - top > 64:
                im = im.crop((left, top, right, bottom)).resize((card_w, card_h), resample=Image.Resampling.LANCZOS)

        if cfg.hue_shift_deg > 0.2 or cfg.tone_jitter_pct > 0.5:
            im = _apply_hue_and_channel_curves(im, cfg.hue_shift_deg, cfg.tone_jitter_pct, r)

        if cfg.tone_jitter_pct > 0.01:
            d = cfg.tone_jitter_pct / 100.0
            im = ImageEnhance.Brightness(im).enhance(r.uniform(1.0 - d, 1.0 + d))
            im = ImageEnhance.Contrast(im).enhance(r.uniform(1.0 - d, 1.0 + d * 1.15))
            im = ImageEnhance.Color(im).enhance(r.uniform(1.0 - d, 1.0 + d * 1.25))
            im = ImageEnhance.Sharpness(im).enhance(r.uniform(max(0.90, 1.0 - d * 1.4), 1.0 + d * 2.2))

        if cfg.light_leak_pct > 0.3:
            im = _apply_diagonal_light_leak(im, cfg.light_leak_pct, r)

        if cfg.chromatic_px > 0:
            rc, gc, bc = im.split()
            sx = r.choice([-cfg.chromatic_px, cfg.chromatic_px])
            sy = r.choice([-1, 0, 1])
            im = Image.merge("RGB", (ImageChops.offset(rc, sx, sy), gc, ImageChops.offset(bc, -sx, -sy)))

        if use_floating_card:
            im = _apply_ambient_floating_frame(
                im, target_w=orig_w, target_h=orig_h, frame_pct=cfg.ambient_frame_pct,
                tilt_deg=cfg.max_tilt_deg, persp_pct=cfg.perspective_pct, r=r,
            )

        if cfg.noise_sigma > 0.05:
            try:
                nw, nh = max(64, orig_w // 2), max(64, orig_h // 2)
                sig = cfg.noise_sigma * 3.4
                noise_rgb = Image.merge(
                    "RGB",
                    (Image.effect_noise((nw, nh), sig), Image.effect_noise((nw, nh), sig), Image.effect_noise((nw, nh), sig)),
                ).resize((orig_w, orig_h), resample=Image.Resampling.BILINEAR)
                im = Image.blend(im, noise_rgb, min(0.075, cfg.noise_sigma * 0.011))
            except Exception:
                pass

        if cfg.vignette_pct > 0.2:
            mask = Image.new("L", (orig_w, orig_h), 0)
            pad_x, pad_y = int(orig_w * r.uniform(0.03, 0.08)), int(orig_h * r.uniform(0.03, 0.08))
            ImageDraw.Draw(mask).ellipse((-pad_x, -pad_y, orig_w + pad_x, orig_h + pad_y), fill=255)
            inv_mask = ImageOps.invert(mask.filter(ImageFilter.GaussianBlur(radius=max(24, min(orig_w, orig_h) // 6))))
            dark = Image.new("RGB", (orig_w, orig_h), (r.randint(4, 18), r.randint(5, 18), r.randint(8, 24)))
            vig_alpha = min(0.36, cfg.vignette_pct / 100.0)
            im.paste(dark, (0, 0), inv_mask.point(lambda p: int(p * vig_alpha)))

        total_flakes = r.randint(cfg.snow_min_flakes, cfg.snow_max_flakes) if cfg.snow_max_flakes > 0 else 0
        if total_flakes > 0:
            base_rgba = im.convert("RGBA")
            scale = max(0.75, min(2.2, min(orig_w, orig_h) / 900.0))
            if not cfg.enable_bokeh and not cfg.enable_wind_trail:
                crisp = Image.new("RGBA", (orig_w, orig_h), (0, 0, 0, 0))
                dc = ImageDraw.Draw(crisp)
                for _ in range(total_flakes):
                    cx, cy = r.uniform(0, orig_w), r.uniform(0, orig_h)
                    rad = r.uniform(0.9, 2.2) * scale
                    dc.ellipse(
                        (cx - rad, cy - rad, cx + rad, cy + rad),
                        fill=(r.randint(245, 255), r.randint(248, 255), 255, r.randint(48, 110)),
                    )
                crisp = crisp.filter(ImageFilter.GaussianBlur(radius=max(0.3, 0.45 * scale)))
                composed = Image.alpha_composite(base_rgba, crisp).convert("RGB")
            else:
                bokeh_l = Image.new("RGBA", (orig_w, orig_h), (0, 0, 0, 0))
                mid_l = Image.new("RGBA", (orig_w, orig_h), (0, 0, 0, 0))
                crisp_l = Image.new("RGBA", (orig_w, orig_h), (0, 0, 0, 0))
                db, dm, dc = ImageDraw.Draw(bokeh_l), ImageDraw.Draw(mid_l), ImageDraw.Draw(crisp_l)
                wind_dx, wind_dy = r.uniform(-3.0, 3.0) * scale, r.uniform(1.5, 4.5) * scale
                for _ in range(total_flakes):
                    cx, cy = r.uniform(0, orig_w), r.uniform(0, orig_h)
                    cr, cg, cb = r.randint(238, 255), r.randint(244, 255), r.randint(248, 255)
                    roll = r.random()
                    if cfg.enable_bokeh and roll < 0.10:
                        rad = r.uniform(7.5, 16.5) * scale
                        in_ctr = (0.18 * orig_w < cx < 0.82 * orig_w) and (0.15 * orig_h < cy < 0.82 * orig_h)
                        al = r.randint(22, 48) if in_ctr else r.randint(46, 96)
                        rx, ry = rad * r.uniform(0.85, 1.20), rad * r.uniform(0.85, 1.20)
                        db.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(cr, cg, cb, al))
                    elif roll < 0.34:
                        rad = r.uniform(2.6, 5.6) * scale
                        al = r.randint(75, 155)
                        rx, ry = rad * r.uniform(0.82, 1.18), rad * r.uniform(0.90, 1.30)
                        dm.ellipse((cx - rx, cy - ry, cx + rx, cy + ry), fill=(cr, cg, cb, al))
                        if cfg.enable_wind_trail and r.random() < 0.45:
                            dm.line([(cx, cy), (cx + wind_dx * 0.9, cy + wind_dy * 0.9)], fill=(255, 255, 255, max(35, al - 45)), width=max(1, int(round(rad * 0.65))))
                    else:
                        rad = r.uniform(1.2, 3.0) * scale
                        al = r.randint(90, 190)
                        dc.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=(cr, cg, cb, al))
                        if cfg.enable_wind_trail and r.random() < 0.25:
                            dc.line([(cx, cy), (cx + wind_dx * 0.55, cy + wind_dy * 0.55)], fill=(255, 255, 255, max(50, al - 50)), width=max(1, int(round(rad * 0.6))))
                if cfg.enable_bokeh:
                    bokeh_l = bokeh_l.filter(ImageFilter.GaussianBlur(radius=max(2.2, 3.2 * scale)))
                mid_l = mid_l.filter(ImageFilter.GaussianBlur(radius=max(0.85, 1.15 * scale)))
                crisp_l = crisp_l.filter(ImageFilter.GaussianBlur(radius=max(0.35, 0.50 * scale)))
                comp_rgba = Image.alpha_composite(base_rgba, bokeh_l) if cfg.enable_bokeh else base_rgba
                comp_rgba = Image.alpha_composite(comp_rgba, mid_l)
                composed = Image.alpha_composite(comp_rgba, crisp_l).convert("RGB")
        else:
            composed = im

        if cfg.border_px > 0:
            bdraw = ImageDraw.Draw(composed)
            bcol = (r.randint(8, 28), r.randint(8, 28), r.randint(12, 34))
            for i in range(cfg.border_px):
                bdraw.rectangle((i, i, orig_w - 1 - i, orig_h - 1 - i), outline=bcol)

        if cfg.canvas_jitter_px > 0:
            dw = r.randrange(-cfg.canvas_jitter_px, cfg.canvas_jitter_px + 1, 2)
            dh = r.randrange(-cfg.canvas_jitter_px, cfg.canvas_jitter_px + 1, 2)
            nw, nh = max(320, orig_w + dw), max(320, orig_h + dh)
            if (nw, nh) != composed.size:
                composed = composed.resize((nw, nh), resample=Image.Resampling.LANCZOS)

        # Scattered 8x8 DCT micro-jitter ensures even solid-color slides differ post-JPEG quantization
        px = composed.load()
        if px is not None:
            cw2, ch2 = composed.size
            for _ in range(24):
                rx, ry = r.randrange(cw2), r.randrange(ch2)
                pixel = list(px[rx, ry])
                ch_i = r.randrange(3)
                pixel[ch_i] = max(0, min(255, pixel[ch_i] + r.choice([-3, -2, 2, 3])))
                px[rx, ry] = tuple(pixel)

        out = io.BytesIO()
        subsampling_mode = r.choice([0, 1, 2]) if cfg.mode == "ultra_hard" else 0
        composed.save(
            out,
            format="JPEG",
            quality=r.randint(cfg.jpeg_quality_min, cfg.jpeg_quality_max),
            subsampling=subsampling_mode,
            optimize=r.choice([True, False]),
        )
        return out.getvalue()


# ── FFmpeg + Pure-Python MP4 Video Uniquifier ────────────────────────────────


def uniquify_mp4_free_atom(raw_bytes: bytes, rng: random.Random | None = None) -> bytes:
    """Pure-Python ISO-BMFF `free` atom SHA-256 randomizer (zero ffmpeg dependency fallback)."""
    r = rng or random.Random()
    pad_len = r.randint(16, 64)
    payload = bytes(r.getrandbits(8) for _ in range(pad_len))
    free_box = struct.pack(">I4s", 8 + len(payload), b"free") + payload
    return raw_bytes + free_box


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
    out_dir = Path(settings.temp_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"vid_{uuid.uuid4().hex[:12]}.mp4"

    # Pure-Python container fallback if ffmpeg/ffprobe are unavailable
    if not shutil.which(settings.ffmpeg_bin) or not shutil.which(settings.ffprobe_bin):
        raw = uniquify_mp4_free_atom(source_path.read_bytes(), rng=r)
        out_path.write_bytes(raw)
        size = len(raw)
        if size > VIDEO_MAX_BYTES:
            out_path.unlink(missing_ok=True)
            raise ValueError(f"Видео превысило лимит 50 МБ ({size / (1024 * 1024):.2f} МБ).")
        return PreparedMedia(
            path=out_path,
            mime_type="video/mp4",
            sha256=_sha256_file(out_path),
            size_bytes=size,
            is_temporary=True,
        )

    width, height, has_audio, src_bitrate = probe_video(source_path, settings.ffprobe_bin)
    target_w = max(2, width - (width % 2))
    target_h = max(2, height - (height % 2))

    crop_ratio = r.uniform(settings.crop_ratio_min, settings.crop_ratio_max)
    brightness = r.uniform(-settings.brightness_shift_max, settings.brightness_shift_max)
    contrast = 1.0 + r.uniform(-settings.contrast_shift_max, settings.contrast_shift_max)
    saturation = 1.0 + r.uniform(-settings.saturation_shift_max, settings.saturation_shift_max)
    speed = 1.0 + r.uniform(-settings.speed_jitter_max, settings.speed_jitter_max)
    crf = r.randint(settings.video_crf_min, settings.video_crf_max)

    vf = (
        f"crop=iw*{crop_ratio:.5f}:ih*{crop_ratio:.5f},"
        f"scale={target_w}:{target_h}:flags=lanczos,"
        f"eq=brightness={brightness:.5f}:contrast={contrast:.5f}:saturation={saturation:.5f},"
        f"setpts={(1.0 / speed):.6f}*PTS"
    )

    encoder = detect_h264_encoder(settings.ffmpeg_bin)
    if encoder == "libx264":
        vcodec_args = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
    elif encoder == "libopenh264":
        base_kbps = (
            max(600, min(8500, src_bitrate // 1000))
            if src_bitrate > 0
            else max(800, int((target_w * target_h) * 0.0018))
        )
        jittered_kbps = int(base_kbps * r.uniform(0.91, 1.09))
        vcodec_args = ["-c:v", "libopenh264", "-b:v", f"{jittered_kbps}k", "-pix_fmt", "yuv420p"]
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

    cmd.extend(["-movflags", "+faststart", str(out_path)])

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
    *,
    mode_override: str | None = None,
    overrides: dict[str, Any] | None = None,
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

    profile = PhotoProfile.resolve(settings, mode_override=mode_override, overrides=overrides)
    disabled = (not settings.enabled) or (profile.mode == "off")

    for src in source_paths:
        if not src.exists():
            raise FileNotFoundError(f"Фото не найдено: {src}")

        if disabled:
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

        uniq_bytes = uniquify_photo_bytes(src.read_bytes(), profile=profile, rng=r)
        if len(uniq_bytes) > PHOTO_MAX_BYTES:
            raise ValueError(
                f"Уникализированное фото {src} превысило лимит 10 МБ ({len(uniq_bytes) / (1024 * 1024):.2f} МБ)."
            )
        out_path = out_dir / f"img_{uuid.uuid4().hex[:12]}.jpg"
        out_path.write_bytes(uniq_bytes)

        prepared.append(
            PreparedMedia(
                path=out_path,
                mime_type="image/jpeg",
                sha256=_sha256_file(out_path),
                size_bytes=len(uniq_bytes),
                is_temporary=True,
            )
        )

    return prepared

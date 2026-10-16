"""Placeholder asset generation (used only when real brand files are not supplied)."""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_FONTS = ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
          "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
          "C:/Windows/Fonts/arialbd.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"]


def _font(size: int):
    for p in _FONTS:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _rgb(hexstr: str) -> tuple[int, int, int]:
    h = hexstr.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore


def monogram(size: int, path: Path, text: str, bg: str, fg: str) -> None:
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=_rgb(bg) + (255,))
    f = _font(int(size * (0.42 if len(text) <= 2 else 0.30)))
    box = d.textbbox((0, 0), text, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((size - w) / 2 - box[0], (size - h) / 2 - box[1]), text, font=f, fill=_rgb(fg) + (255,))
    im.save(path)


def placeholder_screenshot(path: Path, title: str, bg: str, n: int) -> None:
    W, H = 1280, 800
    im = Image.new("RGB", (W, H), (246, 247, 249))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, W, 64], fill=_rgb(bg))
    d.text((28, 18), f"{title}  ·  ChatGPT", font=_font(26), fill="white")
    d.text((60, 110), f"Screenshot {n}", font=_font(34), fill=(20, 20, 20))
    y = 180
    for l in ["User: <example request>", "Plugin: <example response using live tools>", "User: <follow-up>"]:
        d.rounded_rectangle([60, y, W - 60, y + 70], radius=14, fill="white", outline=(220, 222, 226))
        d.text((84, y + 22), l, font=_font(22), fill=(40, 40, 40))
        y += 90
    d.text((60, H - 60), "PLACEHOLDER — replace with a real screenshot of the plugin in ChatGPT before submission",
           font=_font(18), fill=(160, 40, 40))
    im.save(path)

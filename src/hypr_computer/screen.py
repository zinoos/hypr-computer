"""Screen capture (grim), coordinate frames, grid overlay, OCR (tesseract)."""

from __future__ import annotations

import io
import itertools
import math
import os
import re
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageOps

from . import hypr, target

# Claude downsizes images whose long edge exceeds ~1568px or that exceed ~1.15 megapixels. Staying
# under both keeps the image the model sees identical to the frame we map coordinates through.
MAX_EDGE = int(os.environ.get("HYPR_COMPUTER_MAX_EDGE", 1568))
MAX_PIXELS = int(os.environ.get("HYPR_COMPUTER_MAX_PIXELS", 1_150_000))
OCR_LANGS = os.environ.get("HYPR_COMPUTER_OCR_LANGS", "eng")


class ScreenError(RuntimeError):
    pass


@dataclass
class Frame:
    """An image the model has seen, and how its pixels map to Hyprland layout coordinates."""

    id: str
    x: float  # layout box the image covers
    y: float
    w: float
    h: float
    img_w: int
    img_h: int
    label: str
    taken: float = field(default_factory=time.time)
    image: Image.Image | None = None  # full-resolution capture (physical pixels), kept for zoom/OCR/diff

    def to_layout(self, px: float, py: float) -> tuple[float, float]:
        return self.x + px * self.w / self.img_w, self.y + py * self.h / self.img_h

    def from_layout(self, lx: float, ly: float) -> tuple[float, float] | None:
        px = (lx - self.x) * self.img_w / self.w
        py = (ly - self.y) * self.img_h / self.h
        if -0.5 <= px <= self.img_w + 0.5 and -0.5 <= py <= self.img_h + 0.5:
            return px, py
        return None

    @property
    def ratio(self) -> float:
        return self.w / self.img_w


class Frames:
    def __init__(self, keep: int = 30):
        self._frames: dict[str, Frame] = {}
        self._counter = itertools.count(1)
        self._keep = keep
        self.last: Frame | None = None
        self._lock = threading.Lock()

    def new(self, **kw) -> Frame:
        with self._lock:
            f = Frame(id=f"f{next(self._counter)}", **kw)
            self._frames[f.id] = f
            for old in list(self._frames)[:-self._keep]:
                del self._frames[old]
            # Only the newest few keep pixel data.
            for old in list(self._frames.values())[:-4]:
                old.image = None
            self.last = f
            return f

    def get(self, fid: str | None) -> Frame:
        if not fid:
            if self.last is None:
                raise ScreenError("no screenshot taken yet: call screenshot first (or pass coords='screen')")
            return self.last
        f = self._frames.get(fid)
        if not f:
            raise ScreenError(f"unknown frame {fid!r} (kept: {', '.join(self._frames)})")
        return f


# ------------------------------------------------------------------ capture


def grab(box: tuple[float, float, float, float] | None = None, output: str | None = None,
         cursor: bool = False) -> Image.Image:
    args = ["grim", "-t", "ppm"]
    if cursor:
        args.append("-c")
    if output:
        args += ["-o", output]
    elif box:
        x, y, w, h = box
        args += ["-g", f"{round(x)},{round(y)} {max(1, round(w))}x{max(1, round(h))}"]
    args.append("-")
    r = subprocess.run(args, capture_output=True, timeout=15, env=target.env())
    if r.returncode:
        raise ScreenError(f"grim failed: {r.stderr.decode().strip()}")
    return Image.open(io.BytesIO(r.stdout)).convert("RGB")


def fit_size(w: int, h: int, max_edge: int = MAX_EDGE, max_pixels: int = MAX_PIXELS) -> tuple[int, int]:
    s = min(1.0, max_edge / max(w, h), math.sqrt(max_pixels / (w * h)))
    return max(1, int(w * s)), max(1, int(h * s))


def encode(img: Image.Image, fmt: str = "jpeg") -> tuple[bytes, str]:
    buf = io.BytesIO()
    if fmt == "png":
        img.save(buf, "PNG", optimize=False, compress_level=3)
        return buf.getvalue(), "png"
    img.save(buf, "JPEG", quality=88, subsampling=0)
    return buf.getvalue(), "jpeg"


def _font(size: int):
    for path in ("/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
                 "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
                 "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf"):
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def draw_grid(img: Image.Image, step: int = 100) -> Image.Image:
    """Faint grid every `step` image pixels with coordinate labels, to help pick precise points."""
    img = img.convert("RGBA")
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    font = _font(11)
    for x in range(step, img.width, step):
        d.line([(x, 0), (x, img.height)], fill=(255, 0, 140, 70), width=1)
        d.text((x + 2, 1), str(x), fill=(255, 0, 140, 230), font=font, stroke_width=2, stroke_fill=(0, 0, 0, 200))
    for y in range(step, img.height, step):
        d.line([(0, y), (img.width, y)], fill=(255, 0, 140, 70), width=1)
        d.text((2, y + 1), str(y), fill=(255, 0, 140, 230), font=font, stroke_width=2, stroke_fill=(0, 0, 0, 200))
    return Image.alpha_composite(img, over).convert("RGB")


def draw_marks(img: Image.Image, marks: list[tuple[str, float, float, float, float]]) -> Image.Image:
    """Outline labelled boxes (image coords): used to show OCR / accessibility hits."""
    img = img.convert("RGBA")
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    font = _font(12)
    for label, x, y, w, h in marks:
        d.rectangle([x, y, x + w, y + h], outline=(0, 200, 255, 255), width=2)
        d.text((x, max(0, y - 14)), label, fill=(255, 255, 255, 255), font=font, stroke_width=2,
               stroke_fill=(0, 90, 160, 255))
    return Image.alpha_composite(img, over).convert("RGB")


def draw_cursor(img: Image.Image, px: float, py: float) -> Image.Image:
    img = img.copy()
    d = ImageDraw.Draw(img)
    r = 7
    d.ellipse([px - r, py - r, px + r, py + r], outline=(255, 40, 40), width=2)
    d.line([(px - r - 4, py), (px + r + 4, py)], fill=(255, 40, 40), width=1)
    d.line([(px, py - r - 4), (px, py + r + 4)], fill=(255, 40, 40), width=1)
    return img


def changed_fraction(a: Image.Image, b: Image.Image) -> float:
    if a.size != b.size:
        return 1.0
    sa, sb = a.resize((160, 90)).convert("L"), b.resize((160, 90)).convert("L")
    diff = ImageChops.difference(sa, sb).point(lambda v: 255 if v > 24 else 0)
    return sum(1 for v in diff.getdata() if v) / (160 * 90)


# ------------------------------------------------------------------ OCR


@dataclass
class Word:
    text: str
    x: float  # in source-image pixels
    y: float
    w: float
    h: float
    conf: float
    line_key: tuple


@dataclass
class TextHit:
    text: str
    box: tuple[float, float, float, float]  # layout coords
    conf: float
    score: float = 1.0


def ocr_image(img: Image.Image, upscale: float = 2.0, psm: int = 11, langs: str = OCR_LANGS) -> list[list[Word]]:
    """OCR an image; returns lines of words with boxes in the image's own pixel space."""
    g = img.convert("L")
    # Light text on dark backgrounds OCRs much better inverted.
    hist = g.histogram()
    mean = sum(i * c for i, c in enumerate(hist)) / max(1, sum(hist))
    if mean < 110:
        g = ImageOps.invert(g)
    if upscale != 1:
        g = g.resize((int(g.width * upscale), int(g.height * upscale)), Image.LANCZOS)
    buf = io.BytesIO()
    g.save(buf, "PNG", compress_level=1)
    r = subprocess.run(["tesseract", "stdin", "stdout", "--psm", str(psm), "-l", langs, "tsv"],
                       input=buf.getvalue(), capture_output=True, timeout=120)
    if r.returncode:
        raise ScreenError(f"tesseract failed: {r.stderr.decode().strip()[-300:]}")
    lines: dict[tuple, list[Word]] = {}
    for row in r.stdout.decode(errors="replace").splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        text = cols[11].strip()
        conf = float(cols[10])
        if not text or conf < 30:
            continue
        key = (int(cols[2]), int(cols[3]), int(cols[4]))
        x, y, w, h = (int(cols[6]) / upscale, int(cols[7]) / upscale, int(cols[8]) / upscale, int(cols[9]) / upscale)
        lines.setdefault(key, []).append(Word(text, x, y, w, h, conf, key))
    out = [sorted(ws, key=lambda w: w.x) for ws in lines.values()]
    out = _merge_lines(out)
    out.sort(key=lambda ws: (round(ws[0].y / 8), ws[0].x))
    return out


def _merge_lines(lines: list[list[Word]]) -> list[list[Word]]:
    """psm 11 splits a visual line into fragments; join fragments on the same baseline that are close."""
    frags = sorted(lines, key=lambda ws: (ws[0].y, ws[0].x))
    merged: list[list[Word]] = []
    for ws in frags:
        h = max(w.h for w in ws)
        cy = sum(w.y + w.h / 2 for w in ws) / len(ws)
        for m in merged:
            mh = max(w.h for w in m)
            mcy = sum(w.y + w.h / 2 for w in m) / len(m)
            gap = ws[0].x - (m[-1].x + m[-1].w)
            if abs(cy - mcy) < 0.4 * max(h, mh) and -2 < gap < 1.6 * max(h, mh):
                m.extend(ws)
                break
        else:
            merged.append(list(ws))
    return [sorted(m, key=lambda w: w.x) for m in merged]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    return re.sub(r"\s+", " ", s).strip()


def find_in_lines(lines: list[list[Word]], query: str, regex: bool = False, exact: bool = False,
                  fuzzy: float = 0.8) -> list[tuple[list[Word], float]]:
    """Return word runs matching the query (substring, case-insensitive), with a score."""
    hits: list[tuple[list[Word], float]] = []
    q = _norm(query)
    pat = re.compile(query, re.I) if regex else None
    for ws in lines:
        texts = [w.text for w in ws]
        n = len(ws)
        best_line: list[tuple[list[Word], float]] = []
        for i in range(n):
            for j in range(i + 1, min(n, i + 12) + 1):
                cand = " ".join(texts[i:j])
                c = _norm(cand)
                if pat:
                    ok = pat.search(cand) is not None and (j - i == 1 or pat.search(" ".join(texts[i:j - 1])) is None)
                    score = 1.0 if ok else 0.0
                elif exact:
                    score = 1.0 if c.strip(".,:;!?()[]\"'") == q else 0.0
                else:
                    if q in c:
                        # Shortest run containing the query wins.
                        score = 1.0 if (j - i == 1 or q not in _norm(" ".join(texts[i:j - 1]))) and \
                            (j - i == 1 or q not in _norm(" ".join(texts[i + 1:j]))) else 0.0
                    else:
                        score = SequenceMatcher(None, c, q).ratio() if abs(len(c) - len(q)) <= max(3, len(q) // 3) else 0.0
                        if score < fuzzy:
                            score = 0.0
                        else:
                            score *= 0.95
                if score > 0:
                    best_line.append((ws[i:j], score))
        if best_line:
            # Keep non-overlapping best runs in this line.
            best_line.sort(key=lambda t: -t[1])
            used: set[int] = set()
            for run, sc in best_line:
                ids = {id(w) for w in run}
                if ids & used:
                    continue
                used |= ids
                hits.append((run, sc))
    hits.sort(key=lambda t: (-t[1], t[0][0].y, t[0][0].x))
    return hits


def run_box(run: list[Word]) -> tuple[float, float, float, float]:
    x0 = min(w.x for w in run)
    y0 = min(w.y for w in run)
    x1 = max(w.x + w.w for w in run)
    y1 = max(w.y + w.h for w in run)
    return x0, y0, x1 - x0, y1 - y0

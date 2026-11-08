"""Windows built-in OCR (Windows.Media.Ocr) through pywinrt, returning text lines with screen boxes.

Local context: BGRA pixels -> SoftwareBitmap -> OcrEngine.RecognizeAsync -> lines whose box is the union
of their word boxes, shifted to virtual-desktop coordinates.
Global context: used by capture/watcher.py (live screen) and eval/render.py (H3: the OCR channel
experiment runs this exact function). API: https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine
winrt is imported lazily inside the function so it initialises COM (multi-threaded apartment) on the
backend thread, never on Qt's single-threaded UI thread.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from factcheck.models import Box


@dataclass(frozen=True)
class OcrLine:
    text: str
    box: Box


_engines: dict[str, object] = {}


def _engine(language: str):
    from winrt.windows.globalization import Language
    from winrt.windows.media.ocr import OcrEngine

    if language not in _engines:
        engine = OcrEngine.try_create_from_language(Language(language))
        if engine is None:                                    # language pack missing: use the user's languages
            engine = OcrEngine.try_create_from_user_profile_languages()
        if engine is None:
            raise RuntimeError("no Windows OCR language is installed (Settings > Time & language > Language)")
        _engines[language] = engine
    return _engines[language]


async def ocr_bgra(pixels: np.ndarray, origin_x: int = 0, origin_y: int = 0, language: str = "en-US") -> list[OcrLine]:
    """OCR an (H, W, 4) uint8 BGRA image; boxes are returned in absolute coordinates (origin added)."""
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.storage.streams import Buffer

    height, width = pixels.shape[:2]
    limit = OcrEngine.max_image_dimension                     # 10000 px on this machine
    if max(height, width) > limit:
        raise ValueError(f"image {width}x{height} exceeds OCR limit {limit}px")
    data = np.ascontiguousarray(pixels, dtype=np.uint8).tobytes()
    buf = Buffer(len(data))
    buf.length = len(data)                                    # the memoryview spans `length`, so set it first
    memoryview(buf)[:] = data
    bitmap = SoftwareBitmap.create_copy_from_buffer(buf, BitmapPixelFormat.BGRA8, width, height)
    result = await _engine(language).recognize_async(bitmap)
    lines = []
    for line in result.lines:
        rects = [w.bounding_rect for w in line.words]
        if not rects:
            continue
        x0, y0 = min(r.x for r in rects), min(r.y for r in rects)
        x1, y1 = max(r.x + r.width for r in rects), max(r.y + r.height for r in rects)
        lines.append(OcrLine(text=line.text, box=Box(x=origin_x + x0, y=origin_y + y0, w=x1 - x0, h=y1 - y0,
                                                     text=line.text)))
    return lines


def grab(left: int, top: int, width: int, height: int) -> np.ndarray:
    """Screenshot a physical-pixel rectangle as BGRA (mss is created per call: instances are not thread-safe)."""
    import mss

    with mss.mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": width, "height": height})
        return np.frombuffer(shot.raw, dtype=np.uint8).reshape(shot.height, shot.width, 4)

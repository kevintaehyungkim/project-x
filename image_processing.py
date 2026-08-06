"""Screenshot preprocessing before upload to the Claude API.

Raw Retina captures can exceed the API's 10 MB per-image limit. The model
downscales images to ~2576px on the long edge server-side anyway, so this
module pre-does that resize client-side (Lanczos), drops the opaque alpha
plane, and re-encodes as WebP at WEBP_QUALITY — same effective resolution for
the model, a fraction of the upload size.
"""

import os
from io import BytesIO
from pathlib import Path


def _env_long_edge(default: int = 2560) -> int:
    """CAPTUREKIT_MAX_LONG_EDGE, or the default.

    Tunable because the image is by far the largest input cost of a call —
    a 2560x1440 upload is ~4,900 vision tokens, essentially the whole of a
    typical in= count. Anthropic's own recommended long edge is 1568 (~1,850
    tokens), which is less than half the cost.

    The default does not move on that arithmetic alone. Measured A/B on code
    screenshots: at 2560 the model read the target line correctly and captured
    61 lines of surrounding source; at 1568 it read the line one off and
    captured 5. A mis-read line is silent — it produces a confident answer
    about the wrong thing. So lower this only after an A/B on YOUR screenshots
    says the answers are equivalent; that is what the variable is for."""
    try:
        value = int(os.environ.get("CAPTUREKIT_MAX_LONG_EDGE", "") or default)
        return value if value >= 256 else default
    except ValueError:
        return default


MAX_LONG_EDGE = _env_long_edge()   # just under the API's 2576px vision cap
WEBP_QUALITY = 98

_SUFFIX_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _original_bytes(image_path: Path) -> tuple[bytes, str, str]:
    media_type = _SUFFIX_MEDIA_TYPES.get(image_path.suffix.lower(), "image/png")
    with open(image_path, "rb") as f:
        return f.read(), media_type, ""


def prepare_image_for_api(image_path: Path) -> tuple[bytes, str, str]:
    """Downscale so the long edge is <= MAX_LONG_EDGE (only if larger,
    Lanczos), drop alpha, and encode as WebP at WEBP_QUALITY.

    Returns (image_bytes, media_type, detail) where detail is a short
    human-readable summary appended to the "[api] Request sent" stage line.
    Any failure — Pillow missing, unreadable file, encode error — falls back
    to the original file bytes, so the capture loop behaves exactly as it did
    before preprocessing existed.
    """
    try:
        from PIL import Image

        with Image.open(image_path) as img:
            width, height = img.size
            long_edge = max(width, height)
            if long_edge > MAX_LONG_EDGE:
                scale = MAX_LONG_EDGE / long_edge
                new_size = (round(width * scale), round(height * scale))
                img = img.resize(new_size, Image.LANCZOS)
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = BytesIO()
            img.save(buf, format="WEBP", quality=WEBP_QUALITY)
            data = buf.getvalue()
            original_size = image_path.stat().st_size
            detail = (
                f"img {original_size / 1048576:.1f}MB {width}x{height}"
                f" → {len(data) / 1048576:.1f}MB webp"
                f" {img.size[0]}x{img.size[1]} q{WEBP_QUALITY}"
            )
            return data, "image/webp", detail
    except Exception as e:
        data, media_type, _ = _original_bytes(image_path)
        return data, media_type, f"preprocess failed ({e}); sending original"

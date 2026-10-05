"""Profile photo processing: untrusted upload bytes in, a 256px WebP out.

The upload is never stored or served. Every accepted image is fully decoded and
re-encoded, which drops EXIF/GPS metadata and anything appended to or smuggled
inside the file (polyglots), so what we keep is only pixels we produced.

Checks, cheapest first:
  - byte size (the route also caps the stream before it gets here)
  - the decoder itself picks the format from the file's content. Only JPEG, PNG
    and WebP decoders are allowed, so a renamed executable, SVG or GIF fails to
    open no matter what its name or Content-Type claims.
  - dimensions from the header, BEFORE any pixel is decoded, so a small file that
    declares a huge canvas (decompression bomb) is refused without allocating it
  - animated images are refused (we would silently keep frame 1)
  - truncated/corrupt data fails the full decode
"""

from __future__ import annotations

import io
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MIN_SIDE = 64
MAX_SIDE = 4096
OUTPUT_SIDE = 256
ALLOWED_CONTENT_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
_ALLOWED_FORMATS = ("JPEG", "PNG", "WEBP")


class AvatarError(ValueError):
    """The upload is not an acceptable photo. The message is safe to show users."""


def process_avatar(data: bytes) -> bytes:
    """Validate an uploaded photo and return a square OUTPUT_SIDE WebP.

    Raises AvatarError for anything we refuse. CPU-bound: call it off the event
    loop (the route runs it in a threadpool behind a concurrency limit).
    """
    if not data:
        raise AvatarError("The file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise AvatarError("The photo must be 5 MB or smaller.")

    with warnings.catch_warnings():
        # Pillow only WARNS between 1x and 2x MAX_IMAGE_PIXELS; treat it as fatal.
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        try:
            img = Image.open(io.BytesIO(data), formats=_ALLOWED_FORMATS)
        except (UnidentifiedImageError, Image.DecompressionBombError,
                Image.DecompressionBombWarning, OSError, SyntaxError):
            raise AvatarError("Upload a JPEG, PNG or WebP image.") from None

        with img:
            width, height = img.size
            if width > MAX_SIDE or height > MAX_SIDE:
                raise AvatarError("The photo is too large. Use one up to 4096 x 4096 pixels.")
            if width < MIN_SIDE or height < MIN_SIDE:
                raise AvatarError("The photo is too small. Use one at least 64 x 64 pixels.")
            if getattr(img, "is_animated", False):
                raise AvatarError("Animated images are not supported.")
            if img.format == "JPEG":
                # Decode JPEGs at a reduced scale when they are far larger than needed.
                img.draft("RGB", (OUTPUT_SIDE * 2, OUTPUT_SIDE * 2))
            try:
                img.load()
                img = ImageOps.exif_transpose(img)
            except (OSError, SyntaxError, ValueError, Image.DecompressionBombWarning):
                raise AvatarError("The photo could not be read. It may be damaged.") from None

            has_alpha = img.mode in ("RGBA", "LA", "PA") or "transparency" in img.info
            img = img.convert("RGBA" if has_alpha else "RGB")
            # Center crop to a square, then resize. The client already crops to a
            # square; this keeps the output square whatever it sends.
            img = ImageOps.fit(img, (OUTPUT_SIDE, OUTPUT_SIDE), Image.Resampling.LANCZOS)
            out = io.BytesIO()
            img.save(out, format="WEBP", quality=85, method=4)
    return out.getvalue()

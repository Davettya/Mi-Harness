"""Bounded static raster validation shared by uploads and model input."""

from io import BytesIO
import warnings

from PIL import Image, UnidentifiedImageError

from harness.core import HarnessError

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
MIMES = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}


def validate_image(raw, mime):
    if len(raw) > MAX_IMAGE_BYTES:
        raise HarnessError("IMAGE_TOO_LARGE", "图片超过 10 MiB 上限", 413)
    if mime not in MIMES.values():
        raise HarnessError("IMAGE_TYPE", "只支持静态 PNG、JPEG、WebP 图片", 422)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(raw)) as image:
                width, height = image.size
                if MIMES.get(image.format) != mime:
                    raise HarnessError("IMAGE_MIME", "图片内容与声明类型不符", 422)
                if width * height > MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) != 1:
                    raise HarnessError("IMAGE_DIMENSIONS", "图片超过 1600 万像素或包含动画/多帧", 422)
                image.verify()
            with Image.open(BytesIO(raw)) as image:
                image.load()
        return dict(width=width, height=height)
    except HarnessError:
        raise
    except (
        UnidentifiedImageError,
        OSError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ):
        raise HarnessError("IMAGE_INVALID", "无法安全解码图片", 422) from None

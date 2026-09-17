import asyncio
import logging
import re
from io import BytesIO
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from app.config import settings

MAX_BYTES = 2 * 1024 * 1024
MAX_SIZE = 256
_QUALITY = 85
_ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}
_NAME_RE = re.compile(r"^[a-z]+_\d+\.webp$")

logger = logging.getLogger(__name__)


def images_dir() -> Path:
    """Return the directory that stores entity images (next to the database)."""
    return Path(settings.database_path).parent / "images"


def image_path(name: str) -> Path:
    """Resolve a stored image name to a safe path inside the images directory.

    The name must match ``<entity_type>_<id>.webp``; anything else raises
    ValueError so path traversal is impossible.
    """
    if _NAME_RE.fullmatch(name) is None:
        raise ValueError("Недопустимое имя файла изображения")
    root = images_dir().resolve()
    candidate = (root / name).resolve()
    if candidate.parent != root:
        raise ValueError("Недопустимое имя файла изображения")
    return candidate


def image_url(entity_type: str, entity_id: int) -> str:
    """Return the public URL of an entity image."""
    return f"/images/{entity_type}/{entity_id}"


def _normalize(data: bytes) -> Image.Image:
    """Decode and normalize image data; raise ValueError on bad input."""
    try:
        with Image.open(BytesIO(data)) as img:
            source_format = img.format
            if source_format not in _ALLOWED_FORMATS:
                raise ValueError("Недопустимый формат изображения")
            img = ImageOps.exif_transpose(img)
            img = img.convert(
                "RGBA" if source_format in ("PNG", "WEBP") else "RGB"
            )
            width, height = img.size
            side = min(width, height)
            left = (width - side) // 2
            top = (height - side) // 2
            img = img.crop((left, top, left + side, top + side))
            if side > MAX_SIZE:
                img = img.resize((MAX_SIZE, MAX_SIZE), Image.Resampling.LANCZOS)
            return img
    except UnidentifiedImageError as exc:
        raise ValueError("Недопустимый формат изображения") from exc
    except (OSError, EOFError, SyntaxError) as exc:
        raise ValueError("Некорректное изображение") from exc


def _process(data: bytes, entity_type: str, entity_id: int) -> str:
    """Normalize and store image data; return the stored file name."""
    img = _normalize(data)
    name = f"{entity_type}_{entity_id}.webp"
    target = image_path(name)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.unlink(missing_ok=True)
    img.save(target, "WEBP", quality=_QUALITY)
    logger.info("saved image %s", name)
    return name


async def save_image(
    upload: UploadFile | None, entity_type: str, entity_id: int
) -> str | None:
    """Validate, normalize and store an uploaded image; return the file name.

    Returns None for an empty upload. Raises ValueError for files larger than
    2 MiB or in an unsupported or corrupt format.
    """
    if upload is None or not (upload.filename or "").strip():
        return None
    data = await upload.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("Файл больше 2 МБ")
    return await asyncio.to_thread(_process, data, entity_type, entity_id)


async def delete_image(entity_type: str, entity_id: int) -> None:
    """Remove the stored image for an entity, if present."""
    path = image_path(f"{entity_type}_{entity_id}.webp")
    if path.is_file():
        path.unlink(missing_ok=True)
        logger.info("removed image %s_%s.webp", entity_type, entity_id)
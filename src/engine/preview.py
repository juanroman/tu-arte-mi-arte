"""Preview compuesto (PRD §7.5): pegar el conjunto de tres piezas sobre una
foto real de la sala, en los rectángulos calibrados de cada TV.

No dependency on google.adk: this module is testable in isolation and
reusable from any interface.
"""

import io
import logging
import tomllib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from engine import generation
from engine.generation import _save_image_bytes

_logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "room.toml"
REFERENCE_PHOTO_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "room" / "reference.jpg"
)


@dataclass
class PanelRect:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class RoomConfig:
    panels: dict[str, PanelRect]


def load_room_config(path: Path | None = None) -> RoomConfig:
    """Reads the room preview layout (calibrated panel rectangles, as
    fractions of the reference photo's width/height) from an editable TOML
    file."""
    with (path or CONFIG_PATH).open("rb") as f:
        data = tomllib.load(f)
    panels = {name: PanelRect(**rect) for name, rect in data["panels"].items()}
    return RoomConfig(panels=panels)


def _resize_cover(
    image: Image.Image, target_width: int, target_height: int
) -> Image.Image:
    """Resizes `image` to cover a `target_width`x`target_height` box
    (preserving aspect ratio, cropping the overflow), equivalent to CSS
    `object-fit: cover`.
    """
    source_width, source_height = image.size
    scale = max(target_width / source_width, target_height / source_height)
    scaled_width = round(source_width * scale)
    scaled_height = round(source_height * scale)
    resized = image.resize((scaled_width, scaled_height))

    left = (scaled_width - target_width) // 2
    top = (scaled_height - target_height) // 2
    return resized.crop((left, top, left + target_width, top + target_height))


def compose_preview(image_ids: dict[str, str]) -> dict:
    """Composes the room preview (PRD §7.5): pastes each panel's generated
    image into its calibrated rectangle over the real room photo, and saves
    the result to disk under a fresh image_id.

    `image_ids` maps panel name (e.g. '43L', '43R', '50') to the image_id to
    place there. Returns {'image_id': ..., 'path': ...} or {'error': ...} if
    the reference photo is missing, a panel name is unknown, or one of the
    panel images doesn't exist on disk.
    """
    if not REFERENCE_PHOTO_PATH.exists():
        _logger.error("Foto de referencia no encontrada: %s", REFERENCE_PHOTO_PATH)
        return {
            "error": (
                "No existe la foto de referencia de la sala en "
                f"{REFERENCE_PHOTO_PATH}."
            )
        }

    room_config = load_room_config()

    with Image.open(REFERENCE_PHOTO_PATH) as reference:
        canvas = reference.convert("RGB").copy()
    width, height = canvas.size

    for panel_name, image_id in image_ids.items():
        rect = room_config.panels.get(panel_name)
        if rect is None:
            _logger.warning("Panel desconocido en config/room.toml: %s", panel_name)
            return {"error": f"Panel desconocido en config/room.toml: {panel_name!r}."}

        invalid = generation.validate_image_id(image_id)
        if invalid is not None:
            return invalid

        panel_path = generation.IMAGES_DIR / f"{image_id}.jpg"
        if not panel_path.exists():
            _logger.warning(
                "Imagen de panel no encontrada: panel=%s image_id=%s",
                panel_name,
                image_id,
            )
            return {"error": f"No existe una imagen con image_id={image_id!r}."}

        box = (
            round(rect.x0 * width),
            round(rect.y0 * height),
            round(rect.x1 * width),
            round(rect.y1 * height),
        )
        with Image.open(panel_path) as panel_image:
            fitted = _resize_cover(
                panel_image.convert("RGB"), box[2] - box[0], box[3] - box[1]
            )
        canvas.paste(fitted, (box[0], box[1]))

    return _save_image_bytes(_encode_jpeg(canvas))


def _encode_jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()

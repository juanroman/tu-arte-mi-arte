"""Split-mode compensation (PRD §7.3): partir una imagen ancha en las dos
mitades de las 43", recortando la franja central de marco+hueco.

No dependency on google.adk: this module is testable in isolation and
reusable from any interface.
"""

import io
import tomllib
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from engine import generation
from engine.generation import _save_image_bytes

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "split.toml"


@dataclass
class SplitConfig:
    gap_inches: float
    panel_diagonal_inches: float
    wide_aspect_ratio: str

    @property
    def gap_fraction(self) -> float:
        """Fracción del ancho total de la imagen ancha que ocupa la franja
        central, derivada de la geometría física (panel 9:16, diagonal
        panel_diagonal_inches) y el hueco medido (gap_inches)."""
        panel_width = self.panel_diagonal_inches * 9 / (9**2 + 16**2) ** 0.5
        return self.gap_inches / (2 * panel_width + self.gap_inches)


def load_split_config(path: Path | None = None) -> SplitConfig:
    """Reads the split-mode installation constants from an editable TOML
    file."""
    with (path or CONFIG_PATH).open("rb") as f:
        data = tomllib.load(f)
    return SplitConfig(**data)


def split_wide_image(image_id: str, gap_fraction: float) -> dict:
    """Parte una imagen ancha ya generada en dos mitades (43L/43R),
    recortando una franja central de `gap_fraction` del ancho total (la
    franja física de marco+hueco, PRD §7.3). Devuelve {'left': {...},
    'right': {...}} o {'error': ...} si no existe la imagen fuente.
    """
    source_path = generation.IMAGES_DIR / f"{image_id}.jpg"
    if not source_path.exists():
        return {"error": f"No existe una imagen con image_id={image_id!r}."}

    with Image.open(source_path) as source:
        img = source.convert("RGB")
        width, height = img.size
        gap_px = round(width * gap_fraction)
        half_px = (width - gap_px) // 2

        left = img.crop((0, 0, half_px, height))
        right = img.crop((width - half_px, 0, width, height))

    return {
        "left": _save_image_bytes(_encode_jpeg(left), "image/jpeg"),
        "right": _save_image_bytes(_encode_jpeg(right), "image/jpeg"),
    }


def _encode_jpeg(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()

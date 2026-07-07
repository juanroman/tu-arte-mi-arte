"""Wrapper plano sobre google-genai para generar imágenes con Nano Banana 2.

Sin dependencias de google.adk: esta función es testeable de forma aislada
y reutilizable desde cualquier interfaz (adk web hoy, Telegram en Etapa 2).
"""

import uuid
from pathlib import Path

from google import genai
from google.genai import types

IMAGES_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "images"


def generar_imagen_ia(prompt: str, aspect_ratio: str, image_size: str = "1K") -> dict:
    """Genera una imagen con Nano Banana 2 (gemini-3.1-flash-image) a partir de
    un prompt y la guarda en disco. Devuelve el image_id y la ruta del archivo.
    """
    client = genai.Client()
    response = client.models.generate_content(
        model="gemini-3.1-flash-image",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=aspect_ratio, image_size=image_size
            ),
        ),
    )

    content = response.candidates[0].content if response.candidates else None
    parts = content.parts if content else None
    if not parts:
        return {
            "error": "El modelo no devolvió contenido (posible rechazo por política)."
        }

    part = parts[0]
    if not part.inline_data or not part.inline_data.data:
        return {
            "error": "El modelo no devolvió una imagen (posible rechazo por política)."
        }

    image_id = f"img_{uuid.uuid4().hex[:8]}"
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGES_DIR / f"{image_id}.jpg"
    path.write_bytes(part.inline_data.data)

    return {
        "image_id": image_id,
        "path": str(path),
        "mime_type": part.inline_data.mime_type,
    }

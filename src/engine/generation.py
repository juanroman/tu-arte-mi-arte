"""Plain wrapper over google-genai to generate images with Nano Banana 2.

No dependency on google.adk: these functions are testable in isolation and
reusable from any interface (adk web today, Telegram in Etapa 2).
"""

import uuid
from pathlib import Path

from google import genai
from google.genai import types

IMAGES_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "images"


def _new_image_id() -> str:
    return f"img_{uuid.uuid4().hex[:8]}"


def _save_image_bytes(data: bytes, mime_type: str) -> dict:
    """Saves raw image bytes to disk under a fresh image_id and returns its
    metadata. Shared by Gemini-response saves and local (Pillow) saves.
    """
    image_id = _new_image_id()
    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    path = IMAGES_DIR / f"{image_id}.jpg"
    path.write_bytes(data)
    return {"image_id": image_id, "path": str(path), "mime_type": mime_type}


def _save_response_image(response: types.GenerateContentResponse) -> dict:
    """Extracts the generated/edited image from a Gemini response, saves it
    to disk under a fresh image_id, and returns its metadata.
    """
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

    return _save_image_bytes(
        part.inline_data.data, part.inline_data.mime_type or "image/jpeg"
    )


def _load_reference(image_id: str) -> types.Part | dict:
    """Loads a previously saved image as a reference Part for image-to-image
    calls, or an error dict if no image exists under that image_id.
    """
    reference_path = IMAGES_DIR / f"{image_id}.jpg"
    if not reference_path.exists():
        return {"error": f"No existe una imagen con image_id={image_id!r}."}
    return types.Part.from_bytes(
        data=reference_path.read_bytes(), mime_type="image/jpeg"
    )


def generate_image(prompt: str, aspect_ratio: str, image_size: str = "1K") -> dict:
    """Generates an image with Nano Banana 2 (gemini-3.1-flash-image) from a
    prompt and saves it to disk. Returns the image_id and file path.
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
    return _save_response_image(response)


def edit_image(instruction: str, image_id: str, image_size: str = "1K") -> dict:
    """Edits an existing image in place (image-to-image) per PRD §7.7:
    refining is an edit on the reference image, not a regeneration from
    scratch. `instruction` should state what changes and what stays the same.
    Saves the result under a new image_id and returns its metadata.
    """
    reference = _load_reference(image_id)
    if isinstance(reference, dict):
        return reference

    client = genai.Client()
    response = client.models.generate_content(
        model="gemini-3.1-flash-image",
        contents=[reference, instruction],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(image_size=image_size),
        ),
    )
    return _save_response_image(response)

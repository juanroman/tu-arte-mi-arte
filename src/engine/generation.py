"""Plain wrapper over google-genai to generate images with Nano Banana 2.

No dependency on google.adk: these functions are testable in isolation and
reusable from any interface (adk web today, Telegram in Etapa 2).
"""

import uuid
from pathlib import Path

import httpx
from google import genai
from google.genai import errors, types

IMAGES_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "images"

# Reintentos silenciosos (§7.9) para fallas transitorias reales (rate limit,
# 5xx). El backoff exponencial lo maneja el propio SDK.
_RETRY_HTTP_OPTIONS = types.HttpOptions(
    retry_options=types.HttpRetryOptions(
        attempts=3, http_status_codes=[429, 500, 502, 503, 504]
    )
)

# finish_reason que señalan un rechazo real de contenido (política, derechos),
# nunca reintentable con el mismo prompt.
_POLICY_FINISH_REASONS = {
    types.FinishReason.SAFETY,
    types.FinishReason.PROHIBITED_CONTENT,
    types.FinishReason.IMAGE_SAFETY,
    types.FinishReason.IMAGE_PROHIBITED_CONTENT,
    types.FinishReason.BLOCKLIST,
    types.FinishReason.SPII,
    types.FinishReason.RECITATION,
    types.FinishReason.IMAGE_RECITATION,
}

_POLICY_BLOCK_REASONS = {
    types.BlockedReason.SAFETY,
    types.BlockedReason.BLOCKLIST,
    types.BlockedReason.PROHIBITED_CONTENT,
    types.BlockedReason.IMAGE_SAFETY,
    types.BlockedReason.MODEL_ARMOR,
    types.BlockedReason.JAILBREAK,
}


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
    to disk under a fresh image_id, and returns its metadata. If the model
    rejected the request, the error dict is marked with
    `policy_rejection: True` when the SDK signals a real policy/rights block
    (finish_reason o prompt_feedback.block_reason); cualquier otra causa sin
    imagen se reporta como error genérico (§7.9).
    """
    if not response.candidates:
        block_reason = (
            response.prompt_feedback.block_reason if response.prompt_feedback else None
        )
        error: dict = {"error": "El modelo bloqueó la solicitud antes de generar."}
        if block_reason in _POLICY_BLOCK_REASONS:
            error["policy_rejection"] = True
        return error

    candidate = response.candidates[0]
    if candidate.finish_reason in _POLICY_FINISH_REASONS:
        return {
            "error": "El modelo rechazó la solicitud (política o derechos).",
            "policy_rejection": True,
        }

    content = candidate.content
    parts = content.parts if content else None
    if not parts:
        return {"error": "El modelo no devolvió contenido."}

    part = parts[0]
    if not part.inline_data or not part.inline_data.data:
        return {"error": "El modelo no devolvió una imagen."}

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


def _call_model(contents, image_config: types.ImageConfig) -> dict:
    """Calls Nano Banana 2 and saves the resulting image, centralizing retry
    (§7.9: 1-2 reintentos silenciosos ante fallas transitorias, vía el
    HttpRetryOptions nativo del SDK) and exception handling — un
    ClientError/ServerError, o una falla de red (timeout, conexión rechazada)
    que persiste tras los reintentos del SDK, nunca debe propagarse cruda,
    siempre vuelve como {'error': ...}.
    """
    client = genai.Client(http_options=_RETRY_HTTP_OPTIONS)
    try:
        response = client.models.generate_content(
            model="gemini-3.1-flash-image",
            contents=contents,
            config=types.GenerateContentConfig(
                response_modalities=["IMAGE"], image_config=image_config
            ),
        )
    except (errors.ClientError, errors.ServerError) as e:
        return {"error": f"Fallo al llamar al modelo: {e.message or e}"}
    except httpx.RequestError as e:
        return {"error": f"Fallo de red al llamar al modelo: {e}"}
    return _save_response_image(response)


def generate_image(prompt: str, aspect_ratio: str, image_size: str = "1K") -> dict:
    """Generates an image with Nano Banana 2 (gemini-3.1-flash-image) from a
    prompt and saves it to disk. Returns the image_id and file path.
    """
    return _call_model(
        prompt, types.ImageConfig(aspect_ratio=aspect_ratio, image_size=image_size)
    )


def edit_image(instruction: str, image_id: str, image_size: str = "1K") -> dict:
    """Edits an existing image in place (image-to-image) per PRD §7.7:
    refining is an edit on the reference image, not a regeneration from
    scratch. `instruction` should state what changes and what stays the same.
    Saves the result under a new image_id and returns its metadata.
    """
    reference = _load_reference(image_id)
    if isinstance(reference, dict):
        return reference

    return _call_model(
        [reference, instruction], types.ImageConfig(image_size=image_size)
    )


FINAL_HIGH_RES_INSTRUCTION = (
    "Escalado 4K de la imagen de referencia: mantén el layout, la "
    "geometría y la ubicación exacta de los objetos; no introduzcas "
    "elementos nuevos; realza texturas y micro-detalle de forma nativa."
)


def generate_final_high_res(image_id: str) -> dict:
    """Segunda pasada (PRD §7.7): re-genera el draft aprobado en 4K vía
    image-to-image, con una instrucción estricta que preserva layout,
    geometría y contenido — nunca un upscale ciego ni un modelo distinto
    del usado para el draft.
    """
    return edit_image(FINAL_HIGH_RES_INSTRUCTION, image_id, image_size="4K")

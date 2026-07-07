import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from motor.generacion import generar_imagen_ia

JPEG_MAGIC_NUMBER = b"\xff\xd8"

requires_gemini_key = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY no está configurada",
)


@requires_gemini_key
def test_generar_imagen_ia_guarda_jpeg_valido():
    result = generar_imagen_ia("a small red apple on a wooden table", "1:1")

    assert "image_id" in result
    path = Path(result["path"])
    assert path.exists()
    assert path.read_bytes()[:2] == JPEG_MAGIC_NUMBER

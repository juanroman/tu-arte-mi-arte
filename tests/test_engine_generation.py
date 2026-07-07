import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engine import generation
from engine.generation import edit_image, generate_final_high_res, generate_image

JPEG_MAGIC_NUMBER = b"\xff\xd8"

requires_gemini_key = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY no está configurada",
)


@requires_gemini_key
def test_generate_image_saves_valid_jpeg():
    result = generate_image("a small red apple on a wooden table", "1:1")

    assert "image_id" in result
    path = Path(result["path"])
    assert path.exists()
    assert path.read_bytes()[:2] == JPEG_MAGIC_NUMBER


@requires_gemini_key
def test_edit_image_refines_an_existing_image():
    original = generate_image("a small red apple on a wooden table", "1:1")

    result = edit_image(
        "make the apple green, keep everything else the same", original["image_id"]
    )

    assert "image_id" in result
    assert result["image_id"] != original["image_id"]
    path = Path(result["path"])
    assert path.exists()
    assert path.read_bytes()[:2] == JPEG_MAGIC_NUMBER


def test_edit_image_reports_missing_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    result = edit_image("more autumnal", "img_does_not_exist")

    assert "error" in result


@requires_gemini_key
def test_generate_final_high_res_produces_a_new_image():
    draft = generate_image("a small red apple on a wooden table", "1:1")

    result = generate_final_high_res(draft["image_id"])

    assert "image_id" in result
    assert result["image_id"] != draft["image_id"]
    path = Path(result["path"])
    assert path.exists()
    assert path.read_bytes()[:2] == JPEG_MAGIC_NUMBER


def test_generate_final_high_res_reports_missing_reference(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    result = generate_final_high_res("img_does_not_exist")

    assert "error" in result

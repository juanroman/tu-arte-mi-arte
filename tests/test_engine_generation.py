import os
import sys
from pathlib import Path

import httpx
import pytest
from google.genai import errors, types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engine import generation
from engine.generation import edit_image, generate_final_high_res, generate_image

JPEG_MAGIC_NUMBER = b"\xff\xd8"

requires_gemini_key = pytest.mark.skipif(
    not os.environ.get("GEMINI_API_KEY"),
    reason="GEMINI_API_KEY no está configurada",
)


class _FakeModels:
    def __init__(self, response=None, exception=None):
        self._response = response
        self._exception = exception

    def generate_content(self, **kwargs):
        if self._exception is not None:
            raise self._exception
        return self._response


class _FakeClient:
    def __init__(self, response=None, exception=None, **kwargs):
        self.models = _FakeModels(response=response, exception=exception)


def _fake_client_factory(response=None, exception=None):
    def factory(*args, **kwargs):
        return _FakeClient(response=response, exception=exception)

    return factory


def _response_with_finish_reason(finish_reason):
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                finish_reason=finish_reason, content=types.Content(parts=[])
            )
        ]
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


def test_generate_image_flags_policy_rejection(monkeypatch):
    response = _response_with_finish_reason(types.FinishReason.PROHIBITED_CONTENT)
    monkeypatch.setattr(
        generation.genai, "Client", _fake_client_factory(response=response)
    )

    result = generate_image("un tema con derechos", "1:1")

    assert result == {
        "error": "El modelo rechazó la solicitud (política o derechos).",
        "policy_rejection": True,
    }


def test_generate_image_reports_generic_error_without_policy_flag(monkeypatch):
    response = _response_with_finish_reason(types.FinishReason.OTHER)
    monkeypatch.setattr(
        generation.genai, "Client", _fake_client_factory(response=response)
    )

    result = generate_image("un tema cualquiera", "1:1")

    assert "error" in result
    assert "policy_rejection" not in result


def test_generate_image_catches_transient_api_error(monkeypatch):
    exception = errors.ServerError(
        503, {"message": "Service Unavailable", "status": "UNAVAILABLE"}
    )
    monkeypatch.setattr(
        generation.genai, "Client", _fake_client_factory(exception=exception)
    )

    result = generate_image("un tema cualquiera", "1:1")

    assert "error" in result
    assert "policy_rejection" not in result


def test_generate_image_catches_network_connect_error(monkeypatch):
    """The SDK's own retry predicate (tenacity, configured via
    HttpRetryOptions) retries httpx.ConnectError/TimeoutException too, and
    re-raises the raw httpx exception once retries are exhausted — these
    are not genai.errors.ClientError/ServerError subclasses, so _call_model
    must catch them explicitly instead of letting them propagate crudo
    (§7.9 invariant)."""
    exception = httpx.ConnectError("connection refused")
    monkeypatch.setattr(
        generation.genai, "Client", _fake_client_factory(exception=exception)
    )

    result = generate_image("un tema cualquiera", "1:1")

    assert "error" in result
    assert "policy_rejection" not in result


def test_generate_image_catches_network_timeout_error(monkeypatch):
    exception = httpx.TimeoutException("request timed out")
    monkeypatch.setattr(
        generation.genai, "Client", _fake_client_factory(exception=exception)
    )

    result = generate_image("un tema cualquiera", "1:1")

    assert "error" in result
    assert "policy_rejection" not in result

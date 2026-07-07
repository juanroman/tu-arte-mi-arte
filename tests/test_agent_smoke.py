import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "agents"))

from google.adk.agents.llm_agent import Agent
from tu_arte_mi_arte import agent


def test_root_agent_is_well_formed():
    assert isinstance(agent.root_agent, Agent)
    assert agent.root_agent.name
    assert agent.root_agent.model
    assert agent.root_agent.instruction


def test_root_agent_has_generar_imagen_tool():
    tool_names = {tool.__name__ for tool in agent.root_agent.tools}
    assert "generate_image" in tool_names


def test_root_agent_has_refine_image_tool():
    tool_names = {tool.__name__ for tool in agent.root_agent.tools}
    assert "refine_image" in tool_names


def test_root_agent_has_generate_set_tool():
    tool_names = {tool.__name__ for tool in agent.root_agent.tools}
    assert "generate_set" in tool_names


def test_root_agent_has_compose_preview_tool():
    tool_names = {tool.__name__ for tool in agent.root_agent.tools}
    assert "compose_preview" in tool_names


def test_generate_set_produces_the_three_house_panels(monkeypatch):
    ref_calls = []

    def fake_generate_image_ai(prompt, aspect_ratio):
        return {"image_id": "img_43L", "aspect_ratio": aspect_ratio}

    def fake_generate_with_refs_ai(prompt, aspect_ratio, reference_image_ids):
        ref_calls.append((aspect_ratio, tuple(reference_image_ids)))
        image_id = f"img_{aspect_ratio.replace(':', '_')}_{len(ref_calls)}"
        return {"image_id": image_id, "aspect_ratio": aspect_ratio}

    monkeypatch.setattr(agent, "generate_image_ai", fake_generate_image_ai)
    monkeypatch.setattr(agent, "generate_with_refs_ai", fake_generate_with_refs_ai)

    result = agent.generate_set("bicicletas vintage en Santorini")

    assert set(result.keys()) == {"43L", "43R", "50"}
    assert result["43L"]["aspect_ratio"] == "9:16"
    assert result["43R"]["aspect_ratio"] == "9:16"
    assert result["50"]["aspect_ratio"] == "16:9"
    assert result["43L"]["image_id"] != result["50"]["image_id"]

    # 43R se condiciona solo a 43L; la 50 se condiciona al par ya generado.
    assert ref_calls[0] == ("9:16", ("img_43L",))
    assert ref_calls[1] == ("16:9", ("img_43L", result["43R"]["image_id"]))


def test_generate_set_stops_the_chain_on_first_error(monkeypatch):
    def failing_generate_image_ai(prompt, aspect_ratio):
        return {"error": "rechazo por política"}

    def unexpected_generate_with_refs_ai(prompt, aspect_ratio, reference_image_ids):
        raise AssertionError("no debería llamarse tras un error en 43L")

    monkeypatch.setattr(agent, "generate_image_ai", failing_generate_image_ai)
    monkeypatch.setattr(
        agent, "generate_with_refs_ai", unexpected_generate_with_refs_ai
    )

    result = agent.generate_set("un tema con derechos")

    assert set(result.keys()) == {"43L"}
    assert "error" in result["43L"]


def test_generate_set_rejects_unknown_mode():
    result = agent.generate_set("un tema", mode="collage")

    assert "error" in result


def test_generate_set_split_mode_crops_and_orchestrates(monkeypatch):
    from engine.split import SplitConfig

    def fake_generate_image_ai(prompt, aspect_ratio):
        return {"image_id": "img_wide", "aspect_ratio": aspect_ratio}

    def fake_generate_with_refs_ai(prompt, aspect_ratio, reference_image_ids):
        return {
            "image_id": "img_50",
            "aspect_ratio": aspect_ratio,
            "reference_image_ids": tuple(reference_image_ids),
        }

    split_calls = []

    def fake_split_wide_image_ai(image_id, gap_fraction):
        split_calls.append((image_id, gap_fraction))
        return {
            "left": {"image_id": "img_left"},
            "right": {"image_id": "img_right"},
        }

    monkeypatch.setattr(agent, "generate_image_ai", fake_generate_image_ai)
    monkeypatch.setattr(agent, "generate_with_refs_ai", fake_generate_with_refs_ai)
    monkeypatch.setattr(agent, "split_wide_image_ai", fake_split_wide_image_ai)
    monkeypatch.setattr(
        agent,
        "load_split_config",
        lambda: SplitConfig(
            gap_inches=1.0, panel_diagonal_inches=43.0, wide_aspect_ratio="5:4"
        ),
    )

    result = agent.generate_set("faros de playa", mode="split")

    assert set(result.keys()) == {"wide", "43L", "43R", "50"}
    assert result["wide"]["aspect_ratio"] == "5:4"
    assert result["43L"]["image_id"] == "img_left"
    assert result["43R"]["image_id"] == "img_right"
    assert result["50"]["reference_image_ids"] == ("img_wide",)

    assert len(split_calls) == 1
    called_image_id, called_gap_fraction = split_calls[0]
    assert called_image_id == "img_wide"
    assert 0 < called_gap_fraction < 1


def test_generate_set_split_mode_stops_the_chain_if_wide_image_fails(monkeypatch):
    def failing_generate_image_ai(prompt, aspect_ratio):
        return {"error": "rechazo por política"}

    def unexpected_split_wide_image_ai(image_id, gap_fraction):
        raise AssertionError("no debería llamarse tras un error en 'wide'")

    def unexpected_generate_with_refs_ai(prompt, aspect_ratio, reference_image_ids):
        raise AssertionError("no debería llamarse tras un error en 'wide'")

    monkeypatch.setattr(agent, "generate_image_ai", failing_generate_image_ai)
    monkeypatch.setattr(agent, "split_wide_image_ai", unexpected_split_wide_image_ai)
    monkeypatch.setattr(
        agent, "generate_with_refs_ai", unexpected_generate_with_refs_ai
    )

    result = agent.generate_set("un tema con derechos", mode="split")

    assert set(result.keys()) == {"wide"}
    assert "error" in result["wide"]


def test_generate_set_split_mode_stops_the_chain_if_split_fails(monkeypatch):
    def fake_generate_image_ai(prompt, aspect_ratio):
        return {"image_id": "img_wide", "aspect_ratio": aspect_ratio}

    def failing_split_wide_image_ai(image_id, gap_fraction):
        return {"error": "no existe la imagen fuente"}

    def unexpected_generate_with_refs_ai(prompt, aspect_ratio, reference_image_ids):
        raise AssertionError("no debería llamarse tras un error en el split")

    monkeypatch.setattr(agent, "generate_image_ai", fake_generate_image_ai)
    monkeypatch.setattr(agent, "split_wide_image_ai", failing_split_wide_image_ai)
    monkeypatch.setattr(
        agent, "generate_with_refs_ai", unexpected_generate_with_refs_ai
    )

    result = agent.generate_set("un tema", mode="split")

    assert set(result.keys()) == {"wide", "error"}


def test_compose_preview_forwards_the_three_panel_image_ids(monkeypatch):
    captured = {}

    def fake_compose_preview_ai(image_ids):
        captured.update(image_ids)
        return {"image_id": "img_preview", "path": "/tmp/img_preview.jpg"}

    monkeypatch.setattr(agent, "compose_preview_ai", fake_compose_preview_ai)

    result = agent.compose_preview("img_43L", "img_43R", "img_50")

    assert captured == {"43L": "img_43L", "43R": "img_43R", "50": "img_50"}
    assert result["image_id"] == "img_preview"

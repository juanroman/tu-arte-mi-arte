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

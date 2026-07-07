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
    calls = []

    def fake_generate_image_ai(prompt, aspect_ratio):
        calls.append(aspect_ratio)
        return {"image_id": f"img_{aspect_ratio.replace(':', '_')}"}

    monkeypatch.setattr(agent, "generate_image_ai", fake_generate_image_ai)

    result = agent.generate_set("bicicletas vintage en Santorini")

    assert set(result.keys()) == {"43L", "43R", "50"}
    assert result["43L"]["image_id"] != result["50"]["image_id"]
    assert calls.count("9:16") == 2
    assert calls.count("16:9") == 1

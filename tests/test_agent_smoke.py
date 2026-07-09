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


def test_root_agent_has_generate_set_diptico_tool():
    tool_names = {tool.__name__ for tool in agent.root_agent.tools}
    assert "generate_set_diptico" in tool_names


def test_root_agent_has_generate_set_split_tool():
    tool_names = {tool.__name__ for tool in agent.root_agent.tools}
    assert "generate_set_split" in tool_names


def test_root_agent_has_compose_preview_tool():
    tool_names = {tool.__name__ for tool in agent.root_agent.tools}
    assert "compose_preview" in tool_names


def test_generate_set_diptico_produces_the_three_house_panels(monkeypatch):
    calls = []

    def fake_generate_image_ai(prompt, aspect_ratio):
        calls.append((prompt, aspect_ratio))
        image_id = f"img_{len(calls)}"
        return {"image_id": image_id, "aspect_ratio": aspect_ratio}

    monkeypatch.setattr(agent, "generate_image_ai", fake_generate_image_ai)

    result = agent.generate_set_diptico(
        scene_43l="macro de una bicicleta oxidada apoyada en una pared azul",
        scene_43r="figura humana caminando junto a bicicletas estacionadas",
        scene_50="plano general abierto de una calle empedrada con bicicletas",
    )

    assert set(result.keys()) == {"43L", "43R", "50"}
    assert result["43L"]["aspect_ratio"] == "9:16"
    assert result["43R"]["aspect_ratio"] == "9:16"
    assert result["50"]["aspect_ratio"] == "16:9"
    assert result["43L"]["image_id"] != result["50"]["image_id"]

    # cada panel se genera de forma independiente, sin referencias entre sí,
    # a partir de su propia descripción de escena.
    assert len(calls) == 3
    assert "bicicleta oxidada" in calls[0][0]
    assert "caminando junto a bicicletas" in calls[1][0]
    assert "calle empedrada" in calls[2][0]


def test_generate_set_diptico_stops_the_chain_on_first_error(monkeypatch):
    calls = []

    def failing_generate_image_ai(prompt, aspect_ratio):
        calls.append((prompt, aspect_ratio))
        return {"error": "rechazo por política"}

    monkeypatch.setattr(agent, "generate_image_ai", failing_generate_image_ai)

    result = agent.generate_set_diptico(
        scene_43l="un tema con derechos",
        scene_43r="otra escena",
        scene_50="otra escena más",
    )

    assert set(result.keys()) == {"43L"}
    assert "error" in result["43L"]
    assert len(calls) == 1


def test_generate_set_split_crops_and_orchestrates(monkeypatch):
    from engine.split import SplitConfig

    calls = []

    def fake_generate_image_ai(prompt, aspect_ratio):
        calls.append((prompt, aspect_ratio))
        if len(calls) == 1:
            return {"image_id": "img_wide", "aspect_ratio": aspect_ratio}
        return {"image_id": "img_50", "aspect_ratio": aspect_ratio}

    split_calls = []

    def fake_split_wide_image_ai(image_id, gap_fraction):
        split_calls.append((image_id, gap_fraction))
        return {
            "left": {"image_id": "img_left"},
            "right": {"image_id": "img_right"},
        }

    monkeypatch.setattr(agent, "generate_image_ai", fake_generate_image_ai)
    monkeypatch.setattr(agent, "split_wide_image_ai", fake_split_wide_image_ai)
    monkeypatch.setattr(
        agent,
        "load_split_config",
        lambda: SplitConfig(
            gap_inches=1.0, panel_diagonal_inches=43.0, wide_aspect_ratio="5:4"
        ),
    )

    result = agent.generate_set_split(
        scene_wide="faros de playa vistos desde la duna, luz de atardecer",
        scene_50="plano general abierto del muelle de madera al amanecer",
    )

    assert set(result.keys()) == {"wide", "43L", "43R", "50"}
    assert result["wide"]["aspect_ratio"] == "5:4"
    assert result["43L"]["image_id"] == "img_left"
    assert result["43R"]["image_id"] == "img_right"
    assert result["50"]["image_id"] == "img_50"
    assert result["50"]["aspect_ratio"] == "16:9"

    # 50 no lleva referencias — se genera de forma independiente.
    assert len(calls) == 2
    assert "muelle de madera" in calls[1][0]

    assert len(split_calls) == 1
    called_image_id, called_gap_fraction = split_calls[0]
    assert called_image_id == "img_wide"
    assert 0 < called_gap_fraction < 1


def test_generate_set_split_stops_the_chain_if_wide_image_fails(monkeypatch):
    def failing_generate_image_ai(prompt, aspect_ratio):
        return {"error": "rechazo por política"}

    def unexpected_split_wide_image_ai(image_id, gap_fraction):
        raise AssertionError("no debería llamarse tras un error en 'wide'")

    monkeypatch.setattr(agent, "generate_image_ai", failing_generate_image_ai)
    monkeypatch.setattr(agent, "split_wide_image_ai", unexpected_split_wide_image_ai)

    result = agent.generate_set_split(
        scene_wide="un tema con derechos", scene_50="otra escena"
    )

    assert set(result.keys()) == {"wide"}
    assert "error" in result["wide"]


def test_generate_set_split_stops_the_chain_if_split_fails(monkeypatch):
    def fake_generate_image_ai(prompt, aspect_ratio):
        return {"image_id": "img_wide", "aspect_ratio": aspect_ratio}

    def failing_split_wide_image_ai(image_id, gap_fraction):
        return {"error": "no existe la imagen fuente"}

    monkeypatch.setattr(agent, "generate_image_ai", fake_generate_image_ai)
    monkeypatch.setattr(agent, "split_wide_image_ai", failing_split_wide_image_ai)

    result = agent.generate_set_split(scene_wide="un tema", scene_50="otra escena")

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


def test_root_agent_has_finalize_high_res_tool():
    tool_names = {tool.__name__ for tool in agent.root_agent.tools}
    assert "finalize_high_res" in tool_names


def test_finalize_high_res_single_panel_returns_upscaled_image(monkeypatch):
    def fake_generate_final_high_res_ai(image_id):
        return {"image_id": "img_final_43l", "path": "/tmp/img_final_43l.jpg"}

    def unexpected_split_wide_image_ai(image_id, gap_fraction):
        raise AssertionError("no debería llamarse para un panel individual")

    monkeypatch.setattr(
        agent, "generate_final_high_res_ai", fake_generate_final_high_res_ai
    )
    monkeypatch.setattr(agent, "split_wide_image_ai", unexpected_split_wide_image_ai)

    result = agent.finalize_high_res("img_draft_43l")

    assert result == {"image_id": "img_final_43l", "path": "/tmp/img_final_43l.jpg"}


def test_finalize_high_res_split_wide_reruns_split_and_returns_43l_43r(monkeypatch):
    from engine.split import SplitConfig

    def fake_generate_final_high_res_ai(image_id):
        return {"image_id": "img_wide_4k", "path": "/tmp/img_wide_4k.jpg"}

    split_calls = []

    def fake_split_wide_image_ai(image_id, gap_fraction):
        split_calls.append((image_id, gap_fraction))
        return {
            "left": {"image_id": "img_final_43l"},
            "right": {"image_id": "img_final_43r"},
        }

    monkeypatch.setattr(
        agent, "generate_final_high_res_ai", fake_generate_final_high_res_ai
    )
    monkeypatch.setattr(agent, "split_wide_image_ai", fake_split_wide_image_ai)
    monkeypatch.setattr(
        agent,
        "load_split_config",
        lambda: SplitConfig(
            gap_inches=1.0, panel_diagonal_inches=43.0, wide_aspect_ratio="5:4"
        ),
    )

    result = agent.finalize_high_res("img_draft_wide", is_split_wide=True)

    assert result == {
        "43L": {"image_id": "img_final_43l"},
        "43R": {"image_id": "img_final_43r"},
    }
    assert len(split_calls) == 1
    called_image_id, called_gap_fraction = split_calls[0]
    assert called_image_id == "img_wide_4k"
    assert 0 < called_gap_fraction < 1


def test_finalize_high_res_stops_on_upscale_error(monkeypatch):
    def failing_generate_final_high_res_ai(image_id):
        return {"error": "rechazo por política"}

    def unexpected_split_wide_image_ai(image_id, gap_fraction):
        raise AssertionError("no debería llamarse tras un error en el upscale")

    monkeypatch.setattr(
        agent, "generate_final_high_res_ai", failing_generate_final_high_res_ai
    )
    monkeypatch.setattr(agent, "split_wide_image_ai", unexpected_split_wide_image_ai)

    result = agent.finalize_high_res("img_draft_wide", is_split_wide=True)

    assert "error" in result


def test_finalize_high_res_stops_if_split_fails(monkeypatch):
    def fake_generate_final_high_res_ai(image_id):
        return {"image_id": "img_wide_4k", "path": "/tmp/img_wide_4k.jpg"}

    def failing_split_wide_image_ai(image_id, gap_fraction):
        return {"error": "no existe la imagen fuente"}

    monkeypatch.setattr(
        agent, "generate_final_high_res_ai", fake_generate_final_high_res_ai
    )
    monkeypatch.setattr(agent, "split_wide_image_ai", failing_split_wide_image_ai)

    result = agent.finalize_high_res("img_draft_wide", is_split_wide=True)

    assert "error" in result


def test_root_agent_has_deploy_to_panels_tool():
    tool_names = {tool.__name__ for tool in agent.root_agent.tools}
    assert "deploy_to_panels" in tool_names


def test_deploy_to_panels_forwards_image_ids(monkeypatch):
    captured = {}

    def fake_deploy_set_to_panels_ai(image_43l, image_43r, image_50):
        captured["image_43l"] = image_43l
        captured["image_43r"] = image_43r
        captured["image_50"] = image_50
        return {
            "43L": {"content_id": "MY_43L"},
            "43R": {"content_id": "MY_43R"},
            "50": {"content_id": "MY_50"},
        }

    monkeypatch.setattr(agent, "deploy_set_to_panels_ai", fake_deploy_set_to_panels_ai)

    result = agent.deploy_to_panels("img_final_43l", "img_final_43r", "img_final_50")

    assert captured == {
        "image_43l": "img_final_43l",
        "image_43r": "img_final_43r",
        "image_50": "img_final_50",
    }
    assert result == {
        "43L": {"content_id": "MY_43L"},
        "43R": {"content_id": "MY_43R"},
        "50": {"content_id": "MY_50"},
    }

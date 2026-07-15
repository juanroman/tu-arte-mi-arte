import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "agents"))

from google.adk.agents.llm_agent import Agent
from google.adk.tools.skill_toolset import SkillToolset
from tu_arte_mi_arte import agent


def test_root_agent_is_well_formed():
    assert isinstance(agent.root_agent, Agent)
    assert agent.root_agent.name
    assert agent.root_agent.model
    assert agent.root_agent.instruction


def test_root_agent_instruction_macro_archetype_uses_verbose_camera_language():
    """KNOWN_ISSUES.md #2: bare 'macro/detalle' keyword phrasing didn't
    reliably get the model to produce tight macro shots. Guards that the
    macro archetype entry keeps verbose lens/perspective language instead
    of regressing to the old bare-keyword form, without constraining the
    other archetype entries or the 'guía, no lista cerrada' framing.
    """
    instruction = agent.root_agent.instruction

    assert "macro/detalle (" in instruction
    assert "lente" in instruction
    assert "macro/detalle, plano general" not in instruction

    # the other archetypes and the "guide, not closed list" framing must
    # be untouched by this change.
    assert "como guía (no como lista cerrada)" in instruction
    assert "plano general abierto/paisaje" in instruction
    assert "figura humana en la escena" in instruction
    assert "silueta" in instruction
    assert "textura/abstracto en close-up" in instruction
    assert "aéreo/elevado" in instruction
    assert "reflejo/agua" in instruction
    assert "líneas que guían la mirada" in instruction
    assert "luz dorada/contraluz" in instruction


def test_root_agent_has_generar_imagen_tool():
    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
    assert "generate_image" in tool_names


def test_root_agent_has_refine_image_tool():
    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
    assert "refine_image" in tool_names


def test_root_agent_has_generate_set_diptico_tool():
    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
    assert "generate_set_diptico" in tool_names


def test_root_agent_has_generate_set_split_tool():
    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
    assert "generate_set_split" in tool_names


def test_root_agent_has_compose_preview_tool():
    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
    assert "compose_preview" in tool_names


def test_root_agent_has_skill_toolset_for_batch_gallery():
    """dev_plan_phase_2.md 1.1: la skill de galería por lotes se registra
    como un SkillToolset, sin agregar tools de lote sueltas todavía.
    """
    assert any(isinstance(tool, SkillToolset) for tool in agent.root_agent.tools)


def test_root_agent_instruction_disambiguates_temporal_scope_before_concepto():
    """Enmienda post-1.1 (docs/dev_plan_phase_2.md): una conversación real
    (intención de "varios días" revelada gradualmente en 4 turnos, con una
    respuesta ambigua tipo "mas bien un conjunto" en medio) mostró que
    depender solo de la description de la skill no basta — root_agent debe
    preguntar explícitamente el alcance temporal (hoy vs. varios días)
    cuando no sea ya claro, antes de entrar a ETAPA 1 — CONCEPTO.
    """
    instruction = agent.root_agent.instruction

    assert "ALCANCE TEMPORAL" in instruction
    assert "más bien un conjunto" in instruction
    assert instruction.index("ALCANCE TEMPORAL") < instruction.index(
        "ETAPA 1 — CONCEPTO"
    )


def test_root_agent_instruction_and_default_tools_unchanged_by_batch_skill():
    """Requisito duro #10 (dev_plan_phase_2.md): registrar la skill de
    galería por lotes no debe cambiar el comportamiento por defecto de
    root_agent fuera de ese caso de uso — mismo set de tools sueltas, misma
    instrucción, solo se agrega el SkillToolset nuevo encima.
    """
    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
    pre_existing_tool_names = {
        "generate_image",
        "refine_image",
        "generate_set_diptico",
        "generate_set_split",
        "compose_preview",
        "finalize_high_res",
        "deploy_to_panels",
        "revert_tv",
    }
    assert pre_existing_tool_names <= tool_names
    assert "ETAPA 1 — CONCEPTO" in agent.root_agent.instruction
    assert "ETAPA 4 — DESPLIEGUE" in agent.root_agent.instruction


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
    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
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
    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
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


def test_root_agent_has_revert_tv_tool():
    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
    assert "revert_tv" in tool_names


def test_revert_tv_forwards_tv_name(monkeypatch):
    captured = {}

    def fake_revert_tv_ai(tv_name):
        captured["tv_name"] = tv_name
        return {"content_id": "MY_reverted"}

    monkeypatch.setattr(agent, "revert_tv_ai", fake_revert_tv_ai)

    result = agent.revert_tv("43L")

    assert captured == {"tv_name": "43L"}
    assert result == {"content_id": "MY_reverted"}

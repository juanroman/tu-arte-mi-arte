import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "agents"))

from google.adk.agents.llm_agent import Agent
from google.adk.tools.skill_toolset import SkillToolset
from tu_arte_mi_arte import agent


def _resolved_instruction() -> str:
    """root_agent.instruction is a dynamic InstructionProvider (PRD §15:
    prepends the current house date/time on every turn), not a plain
    string — tests need the resolved text, not the callable itself. The
    provider ignores its context argument, so None is a safe stand-in.
    """
    return agent.root_agent.instruction(None)


def test_root_agent_is_well_formed():
    assert isinstance(agent.root_agent, Agent)
    assert agent.root_agent.name
    assert agent.root_agent.model
    assert callable(agent.root_agent.instruction)
    assert _resolved_instruction()


def test_root_agent_instruction_macro_archetype_uses_verbose_camera_language():
    """KNOWN_ISSUES.md #2: bare 'macro/detalle' keyword phrasing didn't
    reliably get the model to produce tight macro shots. Guards that the
    macro archetype entry keeps verbose lens/perspective language instead
    of regressing to the old bare-keyword form, without constraining the
    other archetype entries or the 'guía, no lista cerrada' framing.
    """
    instruction = _resolved_instruction()

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
    preguntar explícitamente el alcance temporal (fijo/permanente vs.
    cambia cada día) cuando no sea ya claro, antes de entrar a ETAPA 1 —
    CONCEPTO. Se evita el fraseo "hoy"/"ahora" para la opción fija porque
    confunde plazo con permanencia.
    """
    instruction = _resolved_instruction()

    assert "ALCANCE TEMPORAL" in instruction
    assert "más bien un conjunto" in instruction
    assert instruction.index("ALCANCE TEMPORAL") < instruction.index(
        "ETAPA 1 — CONCEPTO"
    )


def test_root_agent_instruction_grounds_relative_time_in_current_house_date():
    """Hallazgo de una conversación real (weekend.json): pedir 'el fin de
    semana' llevó al modelo a adivinar qué días eran sábado/domingo, sin
    ninguna señal de la fecha real — y encima el reloj del sistema (2026)
    es posterior al corte de entrenamiento del modelo. root_agent ahora
    antepone la fecha/hora real de la casa (engine.house_clock,
    PRD §15) en cada turno, antes de ALCANCE TEMPORAL, para que 'hoy'/
    'este fin de semana'/'la próxima semana' se resuelvan contra un hecho,
    no una suposición.
    """
    instruction = _resolved_instruction()

    assert "FECHA Y HORA ACTUAL" in instruction
    assert instruction.index("FECHA Y HORA ACTUAL") < instruction.index(
        "ALCANCE TEMPORAL"
    )
    # se recalcula en cada turno, no es un timestamp fijo horneado al importar
    assert callable(agent.root_agent.instruction)


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
    instruction = _resolved_instruction()
    assert "ETAPA 1 — CONCEPTO" in instruction
    assert "ETAPA 4 — DESPLIEGUE" in instruction


def test_batch_skill_has_grouping_proposal_instructions():
    """dev_plan_phase_2.md 1.2: la skill de galería por lotes debe traer
    las instrucciones del paso 2 de PRD §15.3 (propuesta de agrupación en
    2-4 sub-grupos de 2-4 días, default 7 días si no se especifica, y
    aplicar ajustes sin reiniciar la conversación) en el cuerpo cargado
    por load_skill, no solo el placeholder de activación de 1.1.
    """
    skill = agent._galeria_por_lotes_skill
    instructions = skill.instructions

    assert "sub-grupos" in instructions
    assert "2 y 4 sub-grupos" in instructions
    assert "asume 7" in instructions
    assert "reinicies la conversación desde cero" in instructions


def test_batch_skill_has_per_day_prompt_instructions():
    """dev_plan_phase_2.md 1.3: la skill de galería por lotes debe traer
    las instrucciones del paso 4 de PRD §15.3 (redacción de escenas día
    por día dentro de un sub-grupo aprobado, decidiendo por día entre modo
    independiente/díptico de 3 escenas y modo split de 2 escenas según si
    la composición se beneficia de continuidad, sin cuota forzada) y del
    paso 5 (aprobación/corrección por día sin reescribir los demás).
    """
    skill = agent._galeria_por_lotes_skill
    instructions = skill.instructions

    assert "43L, 43R, 50" in instructions
    assert "wide, 50" in instructions
    assert "~70/30" in instructions
    assert "un solo sub-grupo por mensaje" in instructions
    assert "reescribe solo ese día" in instructions


def test_batch_skill_declares_preview_tool_in_additional_tools():
    """dev_plan_phase_2.md 1.4: la skill de galería por lotes debe declarar
    preview_batch_day en metadata.adk_additional_tools de su frontmatter —
    sin esa entrada, SkillToolset nunca resuelve la tool para el modelo
    (PRD §15.4, google/adk/tools/skill_toolset.py
    _resolve_additional_tools_from_state).
    """
    skill = agent._galeria_por_lotes_skill

    assert "preview_batch_day" in skill.frontmatter.metadata.get(
        "adk_additional_tools", []
    )


def test_root_agent_skill_toolset_registers_preview_batch_day():
    """dev_plan_phase_2.md 1.4: preview_batch_day se pasa como
    additional_tools del SkillToolset, no directo en root_agent.tools —
    permanece invisible para el modelo hasta que la skill se activa.
    """
    skill_toolset = next(
        tool for tool in agent.root_agent.tools if isinstance(tool, SkillToolset)
    )

    assert "preview_batch_day" in skill_toolset._provided_tools_by_name

    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
    assert "preview_batch_day" not in tool_names


def test_batch_skill_has_preview_instructions():
    """dev_plan_phase_2.md 1.4: la skill trae las instrucciones del paso 6
    de PRD §15.3 (preview del primer día del sub-grupo por defecto, del
    sub-grupo completo bajo pedido explícito, llamando preview_batch_day).
    """
    skill = agent._galeria_por_lotes_skill
    instructions = skill.instructions

    assert "preview_batch_day" in instructions
    assert "primer día" in instructions
    assert "sub-grupo completo" in instructions


def test_preview_batch_day_dispatches_to_diptico_for_independiente_mode(monkeypatch):
    captured = {}

    def fake_generate_set_diptico(scene_43l, scene_43r, scene_50):
        captured["args"] = (scene_43l, scene_43r, scene_50)
        return {
            "43L": {"image_id": "a"},
            "43R": {"image_id": "b"},
            "50": {"image_id": "c"},
        }

    def unexpected_generate_set_split(scene_wide, scene_50):
        raise AssertionError("no debería llamarse en modo independiente")

    monkeypatch.setattr(agent, "generate_set_diptico", fake_generate_set_diptico)
    monkeypatch.setattr(agent, "generate_set_split", unexpected_generate_set_split)

    result = agent.preview_batch_day(
        mode="independiente",
        scene_43l="escena l",
        scene_43r="escena r",
        scene_50="escena 50",
    )

    assert captured["args"] == ("escena l", "escena r", "escena 50")
    assert result["43L"]["image_id"] == "a"


def test_preview_batch_day_dispatches_to_split_for_split_mode(monkeypatch):
    captured = {}

    def fake_generate_set_split(scene_wide, scene_50):
        captured["args"] = (scene_wide, scene_50)
        return {
            "wide": {"image_id": "w"},
            "43L": {"image_id": "a"},
            "43R": {"image_id": "b"},
            "50": {"image_id": "c"},
        }

    def unexpected_generate_set_diptico(scene_43l, scene_43r, scene_50):
        raise AssertionError("no debería llamarse en modo split")

    monkeypatch.setattr(agent, "generate_set_split", fake_generate_set_split)
    monkeypatch.setattr(agent, "generate_set_diptico", unexpected_generate_set_diptico)

    result = agent.preview_batch_day(
        mode="split", scene_wide="escena ancha", scene_50="escena 50"
    )

    assert captured["args"] == ("escena ancha", "escena 50")
    assert result["wide"]["image_id"] == "w"


def test_batch_skill_resolves_weekend_requests_against_the_real_date():
    """Hallazgo de weekend.json: pedir 'el fin de semana' llevó al modelo a
    adivinar los días sin ninguna fecha real de referencia. La skill ahora
    debe: (a) resolver directo sin preguntar si hoy ya cae viernes-domingo,
    (b) preguntar explícitamente con fechas concretas cuando hoy es
    lunes-jueves, en vez de asumir en silencio una de las dos lecturas
    posibles ('solo sábado y domingo' vs. 'desde hoy hasta el domingo').
    """
    skill = agent._galeria_por_lotes_skill
    instructions = skill.instructions

    assert "fecha actual" in instructions
    assert "viernes, sábado o domingo" in instructions
    assert "lunes a jueves" in instructions
    assert "pregunta explícitamente" in instructions
    assert "la próxima semana" in instructions


def test_root_agent_instruction_forbids_empty_text_after_a_tool_call():
    """Hallazgo de una conversación real (weekend2.json): tras llamar
    list_skills -> load_skill correctamente en el mismo turno, el modelo
    a veces devuelve un texto final vacío (finishReason=STOP, sin ningún
    token de salida) y el usuario se queda sin respuesta visible hasta
    que manda un mensaje de seguimiento. Es un comportamiento estocástico
    del modelo/API (no reproducible a voluntad ni interceptable sin
    reescribir la respuesta del modelo, que se descartó explícitamente
    como solución) — este párrafo es una mitigación de mejor esfuerzo,
    documentada como riesgo aceptado en KNOWN_ISSUES.md, no una garantía.
    """
    instruction = _resolved_instruction()

    assert "Nunca termines un turno sin texto visible" in instruction
    assert "después de llamar una tool" in instruction


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


def test_batch_skill_declares_materialize_tool_in_additional_tools():
    """dev_plan_phase_2.md 2.1: la skill de galería por lotes debe declarar
    materialize_batch_gallery en metadata.adk_additional_tools de su
    frontmatter, igual que preview_batch_day en 1.4 — sin esa entrada,
    SkillToolset nunca resuelve la tool para el modelo.
    """
    skill = agent._galeria_por_lotes_skill

    assert "materialize_batch_gallery" in skill.frontmatter.metadata.get(
        "adk_additional_tools", []
    )


def test_root_agent_skill_toolset_registers_materialize_batch_gallery():
    """dev_plan_phase_2.md 2.1: materialize_batch_gallery se pasa como
    additional_tools del SkillToolset, no directo en root_agent.tools —
    permanece invisible para el modelo hasta que la skill se activa.
    """
    skill_toolset = next(
        tool for tool in agent.root_agent.tools if isinstance(tool, SkillToolset)
    )

    assert "materialize_batch_gallery" in skill_toolset._provided_tools_by_name

    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
    assert "materialize_batch_gallery" not in tool_names


def test_materialize_batch_gallery_rejects_non_consecutive_day_indices():
    result = agent.materialize_batch_gallery(
        theme="Otoño",
        days=[
            {
                "day_index": 1,
                "mode": "independiente",
                "sub_group": "A",
                "prompts": {"43L": "a", "43R": "b", "50": "c"},
            },
            {
                "day_index": 3,
                "mode": "independiente",
                "sub_group": "A",
                "prompts": {"43L": "a", "43R": "b", "50": "c"},
            },
        ],
    )

    assert "error" in result


def test_materialize_batch_gallery_rejects_missing_prompts_for_mode():
    result = agent.materialize_batch_gallery(
        theme="Otoño",
        days=[
            {
                "day_index": 1,
                "mode": "split",
                "sub_group": "A",
                "prompts": {"50": "c"},
            }
        ],
    )

    assert "error" in result


def test_materialize_batch_gallery_rejects_unknown_mode():
    result = agent.materialize_batch_gallery(
        theme="Otoño",
        days=[
            {
                "day_index": 1,
                "mode": "hibrido",
                "sub_group": "A",
                "prompts": {"43L": "a", "43R": "b", "50": "c"},
            }
        ],
    )

    assert "error" in result


def test_materialize_batch_gallery_forwards_approved_days(monkeypatch):
    captured = {}

    def fake_materialize_batch_ai(theme, days):
        captured["theme"] = theme
        captured["days"] = days
        return "batch_abc123"

    monkeypatch.setattr(agent, "materialize_batch_ai", fake_materialize_batch_ai)

    result = agent.materialize_batch_gallery(
        theme="Primavera",
        days=[
            {
                "day_index": 1,
                "mode": "independiente",
                "sub_group": "Sub-grupo A",
                "prompts": {"43L": "a", "43R": "b", "50": "c"},
            },
            {
                "day_index": 2,
                "mode": "split",
                "sub_group": "Sub-grupo A",
                "prompts": {"wide": "d", "50": "e"},
            },
        ],
    )

    assert result == {"batch_id": "batch_abc123", "day_count": 2}
    assert captured["theme"] == "Primavera"
    assert [day.day_index for day in captured["days"]] == [1, 2]
    assert captured["days"][1].prompts == {"wide": "d", "50": "e"}


def test_batch_skill_declares_estimate_duration_tool_in_additional_tools():
    """dev_plan_phase_2.md 2.4: la skill de galería por lotes debe declarar
    estimate_batch_duration en metadata.adk_additional_tools de su
    frontmatter, mismo mecanismo que preview_batch_day/
    materialize_batch_gallery — sin esa entrada, SkillToolset nunca
    resuelve la tool para el modelo.
    """
    skill = agent._galeria_por_lotes_skill

    assert "estimate_batch_duration" in skill.frontmatter.metadata.get(
        "adk_additional_tools", []
    )


def test_root_agent_skill_toolset_registers_estimate_batch_duration():
    """dev_plan_phase_2.md 2.4: estimate_batch_duration se pasa como
    additional_tools del SkillToolset, no directo en root_agent.tools —
    permanece invisible para el modelo hasta que la skill se activa.
    """
    skill_toolset = next(
        tool for tool in agent.root_agent.tools if isinstance(tool, SkillToolset)
    )

    assert "estimate_batch_duration" in skill_toolset._provided_tools_by_name

    tool_names = {getattr(tool, "__name__", None) for tool in agent.root_agent.tools}
    assert "estimate_batch_duration" not in tool_names


def test_estimate_batch_duration_rejects_empty_day_modes():
    result = agent.estimate_batch_duration(day_modes=[])

    assert "error" in result


def test_estimate_batch_duration_rejects_unknown_mode():
    result = agent.estimate_batch_duration(
        day_modes=["independiente", "hibrido", "split"]
    )

    assert "error" in result


def test_estimate_batch_duration_forwards_valid_day_modes(monkeypatch):
    captured = {}

    def fake_estimate_batch_duration_ai(day_modes):
        captured["day_modes"] = day_modes
        return {
            "day_count": 3,
            "independent_days": 2,
            "split_days": 1,
            "total_model_calls": 8,
            "estimated_seconds": 384.0,
            "estimated_minutes": 7,
        }

    monkeypatch.setattr(
        agent, "estimate_batch_duration_ai", fake_estimate_batch_duration_ai
    )

    result = agent.estimate_batch_duration(
        day_modes=["independiente", "split", "independiente"]
    )

    assert captured["day_modes"] == ["independiente", "split", "independiente"]
    assert result["estimated_minutes"] == 7

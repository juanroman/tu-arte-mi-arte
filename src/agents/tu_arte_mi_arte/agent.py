from google.adk.agents.llm_agent import Agent

from motor.generacion import generar_imagen_ia


def generar_imagen(tema: str, aspect_ratio: str = "1:1") -> dict:
    """Genera una imagen con IA a partir de una descripción y la guarda en disco.

    Usa aspect_ratio '9:16' para vertical, '16:9' para horizontal, '1:1' para
    cuadrada.
    """
    return generar_imagen_ia(prompt=tema, aspect_ratio=aspect_ratio)


root_agent = Agent(
    model="gemini-flash-latest",
    name="root_agent",
    description="Asistente de arte generativo para las Samsung Frame TVs de la casa.",
    instruction=(
        "Eres el asistente de arte generativo de la casa. "
        "Puedes generar imágenes con la tool generar_imagen a partir de una "
        "descripción del usuario. Confirma siempre el image_id de lo que generaste."
    ),
    tools=[generar_imagen],
)

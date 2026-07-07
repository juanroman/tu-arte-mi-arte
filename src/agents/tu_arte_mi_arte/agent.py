from google.adk.agents.llm_agent import Agent

root_agent = Agent(
    model='gemini-flash-latest',
    name='root_agent',
    description='Asistente de arte generativo para las Samsung Frame TVs de la casa.',
    instruction=(
        'Eres el asistente de arte generativo de la casa. '
        'Por ahora no tienes herramientas ni capacidades de generación de imágenes; '
        'solo confirma que estás en línea y responde con claridad a lo que te pregunten.'
    ),
)

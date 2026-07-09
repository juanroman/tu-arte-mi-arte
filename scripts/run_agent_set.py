"""One-off driver (dev-only, NOT a pytest test, NOT part of the engine):
drives the real root_agent end-to-end for a single theme — concept already
specific, so it should go straight to generate_set_diptico — then approves
and finalizes to 4K. Prints the final image_id per panel (43L/43R/50) so
they can be fed into scripts/spike_tv_write_path.py.

Hits the real Gemini API, costs money, non-deterministic — run manually:
    uv run python scripts/run_agent_set.py "tema en español"
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "agents"))

from dotenv import load_dotenv  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402
from tu_arte_mi_arte.agent import root_agent  # noqa: E402

load_dotenv()

DEFAULT_THEME = (
    "una obra de construcción urbana de noche: grúas, andamios y "
    "estructuras de concreto iluminadas por reflectores de trabajo"
)


async def send(runner: Runner, user_id: str, session_id: str, text: str) -> list:
    print(f"\n>>> {text}")
    events = []
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        events.append(event)
        for call in event.get_function_calls():
            print(f"  [tool call] {call.name}({call.args})")
        for response in event.get_function_responses():
            print(f"  [tool response] {response.name} -> {response.response}")
        if event.content and event.content.parts:
            for part in event.content.parts:
                if part.text:
                    print(f"  [text] {part.text}")
    return events


def final_tool_results(events: list, tool_names: tuple[str, ...]) -> list[dict]:
    results = []
    for event in events:
        for response in event.get_function_responses():
            if response.name in tool_names:
                results.append(response.response)
    return results


async def main() -> None:
    theme = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_THEME

    user_id = "spike"
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name="tu_arte_mi_arte", user_id=user_id
    )
    runner = Runner(
        app_name="tu_arte_mi_arte", agent=root_agent, session_service=session_service
    )

    await send(runner, user_id, session.id, theme)
    approve_events = await send(
        runner, user_id, session.id, "Apruébalo, súbelo a alta resolución."
    )

    finalize_results = final_tool_results(approve_events, ("finalize_high_res",))
    print("\n=== finalize_high_res results ===")
    for result in finalize_results:
        print(result)


if __name__ == "__main__":
    asyncio.run(main())

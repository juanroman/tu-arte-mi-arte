"""House clock (used to ground relative time references like "hoy" or
"este fin de semana" in root_agent's instruction, PRD §15): reads the
house's timezone from an editable TOML config.

No dependency on google.adk: this module is testable in isolation and
reusable from any interface.
"""

import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "house.toml"

_WEEKDAYS_ES = (
    "lunes",
    "martes",
    "miércoles",
    "jueves",
    "viernes",
    "sábado",
    "domingo",
)

_MONTHS_ES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


@dataclass
class HouseClockConfig:
    timezone: str


def load_house_clock_config(path: Path | None = None) -> HouseClockConfig:
    """Reads the house timezone from an editable TOML file."""
    with (path or CONFIG_PATH).open("rb") as f:
        data = tomllib.load(f)
    return HouseClockConfig(**data)


def current_datetime(config: HouseClockConfig) -> datetime:
    """Current local date/time in the house's timezone."""
    return datetime.now(ZoneInfo(config.timezone))


def describe_now(config: HouseClockConfig) -> str:
    """Renders the current house date/time as a Spanish sentence, e.g.
    'Hoy es miércoles 15 de julio de 2026, 17:10 (hora de
    America/Mexico_City)' — for grounding relative-time phrases in
    root_agent's instruction, not for display to the user verbatim.
    """
    now = current_datetime(config)
    weekday = _WEEKDAYS_ES[now.weekday()]
    month = _MONTHS_ES[now.month - 1]
    return (
        f"Hoy es {weekday} {now.day} de {month} de {now.year}, "
        f"{now.strftime('%H:%M')} (hora de {config.timezone})"
    )

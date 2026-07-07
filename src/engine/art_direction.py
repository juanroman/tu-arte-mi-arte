"""House art direction (PRD §7.8), loaded from an editable TOML config.

No dependency on google.adk: this module is testable in isolation and
reusable from any interface.
"""

import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "art_direction.toml"
)


@dataclass
class ArtDirection:
    style: str
    palette: str
    lighting: str
    grain: str
    tone: str


def load_art_direction(path: Path | None = None) -> ArtDirection:
    """Reads the house art direction from an editable TOML file."""
    with (path or CONFIG_PATH).open("rb") as f:
        data = tomllib.load(f)
    return ArtDirection(**data)


def build_prompt(theme: str, direction: ArtDirection) -> str:
    """Combines the user's instruction with the house style clause, in prose
    (PRD §7.7), not keyword lists.
    """
    return (
        f"{theme}. House art direction: {direction.style}, with a "
        f"{direction.palette} palette, {direction.lighting} and "
        f"{direction.grain}. Overall tone: {direction.tone}."
    )

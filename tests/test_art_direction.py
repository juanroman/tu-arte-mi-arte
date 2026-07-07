import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engine.art_direction import ArtDirection, build_prompt, load_art_direction


def test_load_art_direction_reads_house_config():
    direction = load_art_direction()

    assert direction.style
    assert direction.palette
    assert direction.lighting
    assert direction.grain
    assert direction.tone


def test_load_art_direction_reads_custom_path(tmp_path):
    config_path = tmp_path / "art_direction.toml"
    config_path.write_text(
        'style = "test style"\n'
        'palette = "test palette"\n'
        'lighting = "test lighting"\n'
        'grain = "test grain"\n'
        'tone = "test tone"\n'
    )

    direction = load_art_direction(path=config_path)

    assert direction == ArtDirection(
        style="test style",
        palette="test palette",
        lighting="test lighting",
        grain="test grain",
        tone="test tone",
    )


def test_build_prompt_includes_theme_and_all_fields():
    direction = ArtDirection(
        style="fine art photography",
        palette="soft pastel",
        lighting="golden light",
        grain="film grain",
        tone="serene",
    )

    prompt = build_prompt("a lake at sunset", direction)

    assert "a lake at sunset" in prompt
    assert direction.style in prompt
    assert direction.palette in prompt
    assert direction.lighting in prompt
    assert direction.grain in prompt
    assert direction.tone in prompt


def test_build_prompt_differs_across_art_directions():
    theme = "a lake at sunset"
    pastel = ArtDirection(
        style="fine art photography",
        palette="soft pastel",
        lighting="golden light",
        grain="film grain",
        tone="serene",
    )
    high_contrast = ArtDirection(
        style="editorial photography",
        palette="cold high-contrast",
        lighting="harsh studio light",
        grain="sharp digital clarity",
        tone="dramatic",
    )

    assert build_prompt(theme, pastel) != build_prompt(theme, high_contrast)

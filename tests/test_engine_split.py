import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PIL import Image

from engine import generation
from engine.split import SplitConfig, load_split_config, split_wide_image


def test_load_split_config_reads_house_config():
    config = load_split_config()

    assert config.gap_inches > 0
    assert config.panel_diagonal_inches > 0
    assert config.wide_aspect_ratio


def test_load_split_config_reads_custom_path(tmp_path):
    config_path = tmp_path / "split.toml"
    config_path.write_text(
        "gap_inches = 2.0\n"
        'panel_diagonal_inches = 50.0\nwide_aspect_ratio = "16:9"\n'
    )

    config = load_split_config(path=config_path)

    assert config == SplitConfig(
        gap_inches=2.0, panel_diagonal_inches=50.0, wide_aspect_ratio="16:9"
    )


def test_gap_fraction_is_derived_from_physical_geometry():
    config = SplitConfig(
        gap_inches=1.0, panel_diagonal_inches=43.0, wide_aspect_ratio="5:4"
    )

    assert 0 < config.gap_fraction < 1


def test_split_wide_image_crops_center_gap_and_saves_both_halves(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    width, height = 1000, 800
    wide = Image.new("RGB", (width, height), color="red")
    source_path = tmp_path / "img_wide0000.jpg"
    wide.save(source_path, format="JPEG")

    gap_fraction = 0.1
    result = split_wide_image("img_wide0000", gap_fraction)

    assert "error" not in result
    assert set(result.keys()) == {"left", "right"}

    for side in ("left", "right"):
        assert "image_id" in result[side]
        path = Path(result[side]["path"])
        assert path.exists()

    gap_px = round(width * gap_fraction)
    half_px = (width - gap_px) // 2

    with Image.open(result["left"]["path"]) as left_img:
        assert left_img.size == (half_px, height)
    with Image.open(result["right"]["path"]) as right_img:
        assert right_img.size == (half_px, height)

    assert result["left"]["image_id"] != result["right"]["image_id"]


def test_split_wide_image_reports_missing_source(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    result = split_wide_image("img_does_not_exist", 0.1)

    assert "error" in result


def test_split_wide_image_rejects_gap_fraction_at_or_above_one(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)
    wide = Image.new("RGB", (1000, 800), color="red")
    wide.save(tmp_path / "img_wide0000.jpg", format="JPEG")

    result = split_wide_image("img_wide0000", 1.5)

    assert "error" in result


def test_split_wide_image_rejects_negative_gap_fraction(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)
    wide = Image.new("RGB", (1000, 800), color="red")
    wide.save(tmp_path / "img_wide0000.jpg", format="JPEG")

    result = split_wide_image("img_wide0000", -0.2)

    assert "error" in result


def test_split_wide_image_rejects_malformed_image_id(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    result = split_wide_image("../../../etc/passwd", 0.1)

    assert "error" in result
    assert "inválido" in result["error"]

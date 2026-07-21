import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from PIL import Image

from engine import generation, preview
from engine.preview import PanelRect, RoomConfig, compose_preview, load_room_config


def test_load_room_config_reads_house_config():
    config = load_room_config()

    assert set(config.panels.keys()) == {"43L", "43R", "50"}
    for rect in config.panels.values():
        assert 0 <= rect.x0 < rect.x1 <= 1
        assert 0 <= rect.y0 < rect.y1 <= 1


def test_load_room_config_reads_custom_path(tmp_path):
    config_path = tmp_path / "room.toml"
    config_path.write_text('[panels."43L"]\nx0 = 0.1\ny0 = 0.2\nx1 = 0.3\ny1 = 0.4\n')

    config = load_room_config(path=config_path)

    assert config == RoomConfig(panels={"43L": PanelRect(0.1, 0.2, 0.3, 0.4)})


def _solid_jpeg(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
    Image.new("RGB", size, color=color).save(path, format="JPEG")


def test_compose_preview_pastes_panels_into_room_photo(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    reference_path = tmp_path / "reference.jpg"
    room_width, room_height = 1000, 800
    _solid_jpeg(reference_path, (room_width, room_height), (255, 255, 255))
    monkeypatch.setattr(preview, "REFERENCE_PHOTO_PATH", reference_path)

    room_config = RoomConfig(
        panels={
            "43L": PanelRect(0.0, 0.0, 0.3, 0.5),
            "43R": PanelRect(0.3, 0.0, 0.6, 0.5),
            "50": PanelRect(0.6, 0.5, 1.0, 1.0),
        }
    )
    monkeypatch.setattr(preview, "load_room_config", lambda: room_config)

    _solid_jpeg(tmp_path / "img_left.jpg", (200, 400), (255, 0, 0))
    _solid_jpeg(tmp_path / "img_right.jpg", (200, 400), (0, 255, 0))
    _solid_jpeg(tmp_path / "img_wide.jpg", (400, 200), (0, 0, 255))

    result = compose_preview({"43L": "img_left", "43R": "img_right", "50": "img_wide"})

    assert "error" not in result
    path = Path(result["path"])
    assert path.exists()

    def assert_close(actual, expected, tolerance=5):
        assert all(abs(a - e) <= tolerance for a, e in zip(actual, expected))

    with Image.open(path) as composed:
        assert composed.size == (room_width, room_height)
        assert_close(composed.getpixel((150, 200)), (255, 0, 0))
        assert_close(composed.getpixel((450, 200)), (0, 255, 0))
        assert_close(composed.getpixel((800, 650)), (0, 0, 255))


def test_compose_preview_reports_missing_reference_photo(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(
        preview, "REFERENCE_PHOTO_PATH", tmp_path / "does_not_exist.jpg"
    )

    result = compose_preview({"43L": "img_left"})

    assert "error" in result


def test_compose_preview_reports_missing_panel_image(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    reference_path = tmp_path / "reference.jpg"
    _solid_jpeg(reference_path, (1000, 800), (255, 255, 255))
    monkeypatch.setattr(preview, "REFERENCE_PHOTO_PATH", reference_path)
    monkeypatch.setattr(
        preview,
        "load_room_config",
        lambda: RoomConfig(panels={"43L": PanelRect(0.0, 0.0, 0.3, 0.5)}),
    )

    result = compose_preview({"43L": "img_does_not_exist"})

    assert "error" in result


def test_compose_preview_rejects_malformed_image_id(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    reference_path = tmp_path / "reference.jpg"
    _solid_jpeg(reference_path, (1000, 800), (255, 255, 255))
    monkeypatch.setattr(preview, "REFERENCE_PHOTO_PATH", reference_path)
    monkeypatch.setattr(
        preview,
        "load_room_config",
        lambda: RoomConfig(panels={"43L": PanelRect(0.0, 0.0, 0.3, 0.5)}),
    )

    result = compose_preview({"43L": "../../../etc/passwd"})

    assert "error" in result
    assert "inválido" in result["error"]


def test_compose_preview_reports_unknown_panel_id(tmp_path, monkeypatch):
    monkeypatch.setattr(generation, "IMAGES_DIR", tmp_path)

    reference_path = tmp_path / "reference.jpg"
    _solid_jpeg(reference_path, (1000, 800), (255, 255, 255))
    monkeypatch.setattr(preview, "REFERENCE_PHOTO_PATH", reference_path)
    monkeypatch.setattr(
        preview,
        "load_room_config",
        lambda: RoomConfig(panels={"43L": PanelRect(0.0, 0.0, 0.3, 0.5)}),
    )

    result = compose_preview({"desconocido": "img_left"})

    assert "error" in result

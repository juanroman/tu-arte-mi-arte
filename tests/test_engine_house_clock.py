import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from engine.house_clock import (
    HouseClockConfig,
    current_datetime,
    describe_now,
    load_house_clock_config,
)


def test_load_house_clock_config_reads_house_config():
    config = load_house_clock_config()

    assert config.timezone == "America/Mexico_City"


def test_load_house_clock_config_reads_custom_path(tmp_path):
    config_path = tmp_path / "house.toml"
    config_path.write_text('timezone = "America/Tijuana"\n')

    config = load_house_clock_config(path=config_path)

    assert config == HouseClockConfig(timezone="America/Tijuana")


def test_current_datetime_uses_the_configured_timezone():
    config = HouseClockConfig(timezone="America/Mexico_City")

    now = current_datetime(config)

    assert now.tzinfo is not None
    assert now.utcoffset() == datetime.now(ZoneInfo("America/Mexico_City")).utcoffset()


def test_describe_now_renders_a_spanish_sentence_with_weekday_date_and_time(
    monkeypatch,
):
    import engine.house_clock as house_clock

    fixed_now = datetime(2026, 7, 15, 17, 10, tzinfo=ZoneInfo("America/Mexico_City"))
    monkeypatch.setattr(house_clock, "current_datetime", lambda config: fixed_now)

    sentence = describe_now(HouseClockConfig(timezone="America/Mexico_City"))

    assert "miércoles" in sentence
    assert "15 de julio de 2026" in sentence
    assert "17:10" in sentence
    assert "America/Mexico_City" in sentence

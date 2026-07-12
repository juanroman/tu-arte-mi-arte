import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from engine import tv_discovery
from engine.tv_discovery import (
    CONFIG_PATH,
    TvConfig,
    TvNotFoundError,
    load_tv_configs,
    resolve_tv_host,
)

requires_house_config = pytest.mark.skipif(
    not CONFIG_PATH.exists(),
    reason="config/tvs.toml no está versionado (datos reales de la casa)",
)


def _write_config(path: Path) -> None:
    path.write_text(
        '[tvs."43L"]\n'
        'mac = "AA:AA:AA:AA:AA:AA"\n'
        'last_known_ip = "10.0.0.1"\n'
        '\n[tvs."50"]\n'
        'mac = "BB:BB:BB:BB:BB:BB"\n'
        'last_known_ip = "10.0.0.2"\n'
    )


@requires_house_config
def test_load_tv_configs_reads_house_config():
    configs = load_tv_configs()

    assert set(configs) == {"43L", "43R", "50"}
    for config in configs.values():
        assert config.mac


def test_load_tv_configs_reads_custom_path(tmp_path):
    config_path = tmp_path / "tvs.toml"
    _write_config(config_path)

    configs = load_tv_configs(path=config_path)

    assert configs == {
        "43L": TvConfig(mac="AA:AA:AA:AA:AA:AA", last_known_ip="10.0.0.1"),
        "50": TvConfig(mac="BB:BB:BB:BB:BB:BB", last_known_ip="10.0.0.2"),
    }


def test_resolve_tv_host_uses_cached_ip_when_mac_matches(tmp_path, monkeypatch):
    config_path = tmp_path / "tvs.toml"
    _write_config(config_path)

    monkeypatch.setattr(
        tv_discovery, "_mac_at", lambda ip, timeout: "AA:AA:AA:AA:AA:AA"
    )

    def _fail_browse(timeout):
        raise AssertionError("mDNS browse should not run when the cache hits")

    monkeypatch.setattr(tv_discovery, "_browse_mdns", _fail_browse)

    assert resolve_tv_host("43L", path=config_path) == "10.0.0.1"


def test_resolve_tv_host_falls_back_to_mdns_and_persists_new_ip(tmp_path, monkeypatch):
    config_path = tmp_path / "tvs.toml"
    _write_config(config_path)

    def _mac_at(ip, timeout):
        return "AA:AA:AA:AA:AA:AA" if ip == "10.0.0.99" else None

    monkeypatch.setattr(tv_discovery, "_mac_at", _mac_at)
    monkeypatch.setattr(tv_discovery, "_browse_mdns", lambda timeout: ["10.0.0.99"])

    resolved = resolve_tv_host("43L", path=config_path)

    assert resolved == "10.0.0.99"
    assert load_tv_configs(path=config_path)["43L"].last_known_ip == "10.0.0.99"
    # The other TV's config is untouched by the rewrite.
    assert load_tv_configs(path=config_path)["50"].last_known_ip == "10.0.0.2"


def test_resolve_tv_host_raises_when_no_candidate_matches(tmp_path, monkeypatch):
    config_path = tmp_path / "tvs.toml"
    _write_config(config_path)

    monkeypatch.setattr(tv_discovery, "_mac_at", lambda ip, timeout: None)
    monkeypatch.setattr(tv_discovery, "_browse_mdns", lambda timeout: ["10.0.0.99"])

    with pytest.raises(TvNotFoundError):
        resolve_tv_host("43L", path=config_path)


def test_resolve_tv_host_raises_for_unknown_tv_name(tmp_path):
    config_path = tmp_path / "tvs.toml"
    _write_config(config_path)

    with pytest.raises(TvNotFoundError):
        resolve_tv_host("does-not-exist", path=config_path)

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from engine import tv_discovery
from engine.tv_discovery import (
    CONFIG_PATH,
    TvConfig,
    TvNotFoundError,
    _save_last_known_ip,
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


def test_resolve_tv_host_uses_default_discovery_timeout_for_mdns_browse(
    tmp_path, monkeypatch
):
    """A stale cache should fall back to mDNS with the module's full
    DEFAULT_DISCOVERY_TIMEOUT_SECONDS window, not some other value —
    regression for the real 2026-07-14 failure where a 5.0s window was too
    tight for the 50" TV's mDNS announcement to land in time.
    """
    config_path = tmp_path / "tvs.toml"
    _write_config(config_path)

    monkeypatch.setattr(tv_discovery, "_mac_at", lambda ip, timeout: None)

    seen_timeouts = []

    def _browse_mdns(timeout):
        seen_timeouts.append(timeout)
        return []

    monkeypatch.setattr(tv_discovery, "_browse_mdns", _browse_mdns)

    with pytest.raises(TvNotFoundError):
        resolve_tv_host("43L", path=config_path)

    assert seen_timeouts == [tv_discovery.DEFAULT_DISCOVERY_TIMEOUT_SECONDS]


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


def test_browse_mdns_listens_for_the_full_timeout_budget(monkeypatch):
    """resolve_tv_host passes its whole timeout budget down expecting
    _browse_mdns to spend it listening for mDNS announcements — a TV that's
    slow to announce itself (e.g. just woken up) needs that full window.
    _browse_mdns silently discarded most of it by hardcoding
    `min(2.5, timeout)`, so a caller asking for timeout=5.0 only ever got
    2.5s of real listening time (confirmed live 2026-07-13: the 50" TV
    intermittently failed discovery under exactly this kind of timing
    pressure). Assert the full budget is actually used, not silently capped.
    """
    monkeypatch.setattr(tv_discovery, "Zeroconf", lambda: _FakeZeroconf())
    monkeypatch.setattr(
        tv_discovery, "ServiceBrowser", lambda zc, service_type, listener: None
    )

    slept = {}
    monkeypatch.setattr(
        tv_discovery.time, "sleep", lambda seconds: slept.setdefault("seconds", seconds)
    )

    tv_discovery._browse_mdns(timeout=5.0)

    assert slept["seconds"] == 5.0


class _FakeZeroconf:
    def close(self):
        pass


def test_concurrent_saves_for_different_tvs_do_not_lose_either_update(
    tmp_path, monkeypatch
):
    """_save_last_known_ip does a read-modify-write of the whole config
    file. Two TVs resolving concurrently (deploy_set_to_panels runs all
    three in a ThreadPoolExecutor) can both read the same stale snapshot
    and then each write back the full file with only their own TV
    updated -- last writer wins, silently discarding the other's freshly
    confirmed IP. A lock around the read-modify-write critical section
    must serialize the two calls so neither update is lost.
    """
    config_path = tmp_path / "tvs.toml"
    _write_config(config_path)

    thread_1_reading = threading.Event()
    release_thread_1 = threading.Event()
    real_load_tv_configs = tv_discovery.load_tv_configs
    first_call_done = False

    def _load_tv_configs_pausing_first_caller(path):
        nonlocal first_call_done
        configs = real_load_tv_configs(path)
        if not first_call_done:
            first_call_done = True
            thread_1_reading.set()
            release_thread_1.wait(timeout=5)
        return configs

    monkeypatch.setattr(
        tv_discovery, "load_tv_configs", _load_tv_configs_pausing_first_caller
    )

    thread_1 = threading.Thread(
        target=_save_last_known_ip, args=("43L", "10.0.0.111", config_path)
    )
    thread_1.start()
    assert thread_1_reading.wait(timeout=5)

    # Give thread_2 a real window to run to completion (buggy code: no
    # lock, so it finishes here comfortably) or to block on the fix's
    # lock (in which case this join simply times out without completing).
    thread_2 = threading.Thread(
        target=_save_last_known_ip, args=("50", "10.0.0.222", config_path)
    )
    thread_2.start()
    thread_2.join(timeout=0.3)
    release_thread_1.set()
    thread_1.join(timeout=5)
    thread_2.join(timeout=5)

    configs = load_tv_configs(path=config_path)
    assert configs["43L"].last_known_ip == "10.0.0.111"
    assert configs["50"].last_known_ip == "10.0.0.222"


def test_save_last_known_ip_does_not_corrupt_file_when_replace_fails(
    tmp_path, monkeypatch
):
    """A failure during the final atomic swap (e.g. the disk fills up
    mid-operation) must never leave `tvs.toml` truncated or invalid: the
    new content must be written to a temp file first and only swapped in
    via an atomic replace, never a direct in-place overwrite.
    """
    config_path = tmp_path / "tvs.toml"
    _write_config(config_path)
    original_content = config_path.read_text()

    def _failing_replace(*args, **kwargs):
        raise OSError("disco lleno durante el reemplazo atómico")

    monkeypatch.setattr(tv_discovery.os, "replace", _failing_replace)

    with pytest.raises(OSError):
        _save_last_known_ip("43L", "10.0.0.99", config_path)

    assert config_path.read_text() == original_content

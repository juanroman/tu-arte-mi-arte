"""Resolve a Samsung Frame TV's current LAN IP by its stable MAC address
(PRD §7.6, §7.10). DHCP can reassign a TV's IP at any time (observed live
2026-07-08: Frame 50 moved from .103 to .104) — this module lets the rest of
the system dial a TV by name without trusting a hardcoded/reserved IP to
never drift.

Strategy: try the cached `last_known_ip` first via a cheap REST call
(`GET /api/v2/`, which every TV answers without a token) and confirm its
`wifiMac` still matches; if that fails, browse mDNS (`_samsungmsf._tcp`,
the service type used during the original discovery phase, PRD Apéndice A)
for candidate IPs and REST-confirm each one's MAC the same way. A
freshly-confirmed IP is persisted back to `config/tvs.toml` so the next
call is fast again.

No dependency on google.adk: this module is testable in isolation and
reusable from any interface.
"""

import socket
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

import requests
from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "tvs.toml"
MDNS_SERVICE_TYPE = "_samsungmsf._tcp.local."


class TvNotFoundError(Exception):
    """No TV matching the configured MAC could be reached, neither at its
    last known IP nor via mDNS discovery."""


@dataclass
class TvConfig:
    mac: str
    last_known_ip: str | None


def load_tv_configs(path: Path | None = None) -> dict[str, TvConfig]:
    """Reads the known TVs (name -> stable MAC + cached IP) from an
    editable TOML file."""
    with (path or CONFIG_PATH).open("rb") as f:
        data = tomllib.load(f)
    return {name: TvConfig(**fields) for name, fields in data["tvs"].items()}


_TOML_HEADER = (
    "# TVs conocidas de la casa (PRD §7.6, §7.10). La MAC es la clave "
    "estable —\n"
    "# la IP la asigna DHCP y puede cambiar. last_known_ip es solo una "
    "caché,\n"
    "# mantenida al día automáticamente por resolve_tv_host().\n"
)


def _save_last_known_ip(name: str, ip: str, path: Path | None = None) -> None:
    target = path or CONFIG_PATH
    configs = load_tv_configs(target)
    configs[name].last_known_ip = ip
    lines = [_TOML_HEADER, ""]
    for tv_name, config in configs.items():
        lines.append(f'[tvs."{tv_name}"]')
        lines.append(f'mac = "{config.mac}"')
        ip_value = f'"{config.last_known_ip}"' if config.last_known_ip else '""'
        lines.append(f"last_known_ip = {ip_value}")
        lines.append("")
    target.write_text("\n".join(lines))


def _mac_at(ip: str, timeout: float) -> str | None:
    """Returns the wifiMac reported by the TV at `ip`, or None if it's
    unreachable or doesn't answer the Samsung REST API."""
    try:
        response = requests.get(f"http://{ip}:8001/api/v2/", timeout=timeout)
        response.raise_for_status()
        return str(response.json()["device"]["wifiMac"])
    except (requests.RequestException, KeyError, ValueError):
        return None


class _CollectNames(ServiceListener):
    def __init__(self) -> None:
        self.names: set[str] = set()

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.names.add(name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self.names.add(name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        pass


def _browse_mdns(timeout: float) -> list[str]:
    """Browses `_samsungmsf._tcp` and returns the IPs of every Samsung TV
    found within `timeout` seconds.

    Uses zeroconf's ServiceBrowser only to enumerate candidate service
    names — resolving each name's address via zeroconf's own
    ServiceInfo.request() proved unreliable in practice (silently failed
    for 2 of 3 real TVs on this network, repeatedly, even with long
    timeouts and retries). Resolving the equivalent `<name>.local`
    hostname via the stdlib's socket.gethostbyname() — which defers to the
    OS's own mDNS resolver (e.g. macOS's Bonjour) instead of zeroconf's
    pure-Python one — was reliable and fast for all three TVs across
    repeated runs, so that's the resolution step used here.
    """
    zc = Zeroconf()
    try:
        listener = _CollectNames()
        ServiceBrowser(zc, MDNS_SERVICE_TYPE, listener)
        time.sleep(timeout)
    finally:
        zc.close()

    ips: list[str] = []
    for name in listener.names:
        hostname = name.removesuffix(MDNS_SERVICE_TYPE) + "local"
        try:
            ips.append(socket.gethostbyname(hostname))
        except OSError:
            continue
    return ips


def resolve_tv_host(name: str, path: Path | None = None, timeout: float = 5.0) -> str:
    """Returns a reachable IP for the TV `name` ("43L"/"43R"/"50"),
    confirmed live by matching its wifiMac against config/tvs.toml.

    Tries the cached last_known_ip first; if it's stale or missing, browses
    mDNS for candidate IPs and confirms each by MAC. A freshly confirmed IP
    is persisted back to config so the next call skips the mDNS browse.
    Raises TvNotFoundError if no candidate IP has a matching MAC.
    """
    configs = load_tv_configs(path)
    if name not in configs:
        raise TvNotFoundError(f"No hay una TV configurada con nombre {name!r}.")
    config = configs[name]

    if config.last_known_ip and _mac_at(config.last_known_ip, timeout) == config.mac:
        return config.last_known_ip

    for candidate_ip in _browse_mdns(timeout):
        if _mac_at(candidate_ip, timeout) == config.mac:
            _save_last_known_ip(name, candidate_ip, path)
            return candidate_ip

    raise TvNotFoundError(
        f"No se pudo alcanzar la TV {name!r} (mac={config.mac}) ni por su "
        f"última IP conocida ni por mDNS."
    )

"""Stable aliases for filtering historical notes without rewriting their scope."""

from memd.config import host_names

# Values are placeholder host roles; MEMD_HOST_NAMES maps them to real names.
_ALIASES = {
    "apphost": "apphost", "10.10.1.10": "apphost",
    "hass": "homeassistant", "homeassistant": "homeassistant",
    "home-assistant": "homeassistant", "10.10.140.20": "homeassistant",
    "unifi-gateway": "unifi-gw", "unifi-gw": "unifi-gw", "10.10.1.1": "unifi-gw",
}


def canonical_host(value: str) -> str:
    host = value.strip().casefold()
    for suffix in (".local", ".lan"):
        if host.endswith(suffix):
            host = host[: -len(suffix)]
    names = host_names()
    role = _ALIASES.get(host, host)
    return names.get(role, role)

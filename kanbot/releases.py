"""Validate version-only metadata carried by existing swarm API responses."""
import re
from . import __version__


def valid_version(value):
    return isinstance(value, str) and re.fullmatch(r"(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})\.(0|[1-9]\d{0,5})", value) is not None


def api_releases(value):
    if not isinstance(value, dict) or value.get("protocol") != 1 or not all(valid_version(value.get(k)) for k in ("plugin", "kanbot")):
        return None
    return {k: value[k] for k in ("protocol", "plugin", "kanbot")}


def release_status(receipt):
    releases = api_releases(receipt.get("releases")) if isinstance(receipt, dict) else None
    if not releases:
        return {"installed": __version__, "status": "unknown", "updateAvailable": False}
    newer = tuple(map(int, releases["kanbot"].split('.'))) > tuple(map(int, __version__.split('.')))
    return {"installed": __version__, "latest": releases["kanbot"], "plugin": releases["plugin"],
            "status": "received", "source": "swarm-api", "receivedAt": receipt.get("receivedAt"), "updateAvailable": newer}

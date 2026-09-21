"""Tell the user when a newer Kanbot is on PyPI. Never installs anything."""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request

from . import __version__
from .config import config_dir

URL = "https://pypi.org/pypi/kanbot/json"


def latest_version() -> str | None:
    """Newest version on PyPI, asked at most once a day. Never raises, never prints."""
    if os.environ.get("KANBOT_NO_UPDATE_CHECK"):
        return None
    latest = None
    try:
        path = config_dir() / "update-check.json"
        checked = 0.0
        try:
            cache = json.loads(path.read_text())
            if isinstance(cache["latest"], (str, type(None))):  # None: never got an answer yet
                latest, checked = cache["latest"], float(cache["checked"])
        except Exception:
            pass  # no cache, or a corrupt one
        if 0 <= time.time() - checked < 86400:
            return latest
        try:
            request = urllib.request.Request(URL, headers={"User-Agent": f"kanbot/{__version__}"})
            with urllib.request.urlopen(request, timeout=2) as response:
                latest = str(json.load(response)["info"]["version"])
        except Exception:
            pass  # offline counts as checked: wait 2 s once a day, not on every command
        # ponytail: plain write, no lock. Two commands at once can tear the file;
        # a torn file reads as no cache and costs one more request.
        path.write_text(json.dumps({"checked": time.time(), "latest": latest}))
    except Exception:
        pass
    return latest


def _key(version: str) -> tuple:
    # ponytail: first three number groups only, so "1.0rc1" equals "1.0".
    # Ceiling: a user on a pre-release is not told about its final release.
    return tuple(int(n or 0) for n in re.match(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", version).groups())


def newer(latest: str | None) -> bool:
    try:
        return bool(latest) and _key(latest) > _key(__version__)
    except Exception:
        return False


def update_notice() -> str:
    latest = latest_version()
    if not newer(latest):
        return ""
    return (f"Kanbot {latest} is out (you have {__version__}). "
            "Update: uv tool upgrade kanbot  (or: pipx upgrade kanbot)")

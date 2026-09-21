"""Tell the user when a newer Kanbot is on PyPI. Never installs anything."""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request

from . import __version__
from .config import config_dir

URL = "https://pypi.org/pypi/kanbot/json"
LIMIT = 2  # seconds, for the whole request


def _sane(version) -> bool:
    # The answer and the cache are outside input, and the notice prints this text.
    return isinstance(version, str) and bool(re.fullmatch(r"[0-9A-Za-z.!+_-]{1,40}", version))


def _ask(answer: list) -> None:
    try:
        request = urllib.request.Request(URL, headers={"User-Agent": f"kanbot/{__version__}"})
        with urllib.request.urlopen(request, timeout=2) as response:
            answer.append(json.loads(response.read(1 << 20))["info"]["version"])
    except Exception:
        pass


def latest_version() -> str | None:
    """Newest version on PyPI, asked at most once a day (once an hour after a miss). Never raises, never prints."""
    if os.environ.get("KANBOT_NO_UPDATE_CHECK"):
        return None
    latest = None
    try:
        path = config_dir() / "update-check.json"
        checked, wait = 0.0, 86400
        try:
            cache = json.loads(path.read_text())
            if cache["latest"] is None or _sane(cache["latest"]):  # None: never got an answer yet
                latest, checked = cache["latest"], float(cache["checked"])
                wait = 86400 if cache.get("ok", True) else 3600  # no answer last time: ask again in an hour
        except Exception:
            pass  # no cache, or a corrupt one
        if 0 <= time.time() - checked < wait:
            return latest
        # urlopen's timeout is per socket step and skips DNS, so the thread is the
        # real limit. No answer counts as checked too: wait 2 s once an hour, not on every command.
        answer: list = []
        thread = threading.Thread(target=_ask, args=(answer,), daemon=True)
        thread.start()
        thread.join(LIMIT)
        ok = bool(answer and _sane(answer[0]))
        if ok:
            latest = answer[0]
        # Replace, not write: a second command never reads a half-written file.
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"checked": time.time(), "latest": latest, "ok": ok}))
        os.replace(tmp, path)
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
            "Update: uv tool upgrade kanbot  (or: pipx upgrade kanbot, or: uvx kanbot@latest)")

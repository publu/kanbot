"""Prompt modes (profiles) — reusable instruction preludes prepended to a card's
prompt before it's sent to the agent (and re-applied on every loop iteration, so
the behavior survives fresh-context Ralph loops).

A mode is just a name + a block of guidance. Add your own here or via config.
"""
from __future__ import annotations

from typing import Dict, List

# The "lean" mode is our take on a minimal-code / reuse-first working style:
# favor the smallest change that works, delete more than you add.
LEAN = """[lean mode] Favor the smallest change that works. Before writing any code, go down this ladder in order and stop at the first that applies:
1. Don't build it unless it's actually needed right now (YAGNI). Question the requirement first.
2. Use the standard library before anything else.
3. Use built-in / native platform or framework features.
4. Reuse code, helpers, and dependencies already in this project.
5. Prefer the minimal solution — a few lines over a new function, a function over a new module/file.
6. Only as a last resort write new code, and keep it to the minimum that works.
Rules: no speculative abstraction, no new dependencies without a clear need, no copy-paste duplication. When you can solve it by deleting code, do that. Match the surrounding code's style. Touch as few files as possible."""

PROFILES: Dict[str, Dict[str, str]] = {
    "lean": {
        "name": "lean",
        "label": "Lean — write the least code",
        "description": "Reuse-first, YAGNI, minimal-diff working style. Cheaper, faster, less bloat.",
        "prelude": LEAN,
    },
}


def list_profiles() -> List[Dict[str, str]]:
    return [{"name": p["name"], "label": p["label"], "description": p["description"]}
            for p in PROFILES.values()]


def compose_prompt(profile: str, prompt: str) -> str:
    """Prepend a mode's prelude to the prompt, if the mode exists."""
    p = PROFILES.get((profile or "").strip())
    if not p:
        return prompt
    return f"{p['prelude']}\n\n---\n\n{prompt}" if prompt else p["prelude"]

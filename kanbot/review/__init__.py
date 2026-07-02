"""AI-native code review engine, ported to run on KanBot's CLI agents.

Public API:
    from kanbot.review import review, ReviewConfig
    out = await review(diff_text, repo_path=".")
    print(out.markdown)   # event: out.event, findings: out.findings
"""
from .config import ReviewConfig
from .pipeline import ReviewOutput, gate_review, review

__all__ = ["review", "gate_review", "ReviewConfig", "ReviewOutput"]

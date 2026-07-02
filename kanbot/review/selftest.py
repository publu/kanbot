"""Runnable checks for the deterministic core — no LLM, no network.

Covers the parts that break silently: diff parsing, JSON extraction from messy
agent output, scoring/threshold filtering, and the merge-gate review event.

    python -m kanbot.review.selftest
"""
from __future__ import annotations

from .config import ScoringConfig
from .diff_engine import compute_diff_stats, parse_unified_diff
from .pipeline import _split_patches
from .runtime import _extract_json
from .schemas.output import ScoredFinding
from .schemas.pipeline import AdversaryResult, ReviewFinding
from .scoring import determine_review_event, score_findings

_DIFF = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -1,3 +1,4 @@
 import os
+import sys
 def main():
-    return 1
+    return 0
diff --git a/new.py b/new.py
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+x = 1
+y = 2
"""


def demo() -> None:
    # diff parsing
    files = parse_unified_diff(_DIFF)
    assert len(files) == 2, files
    assert files[0].path == "app.py" and files[1].status == "added", files
    stats = compute_diff_stats(files)
    assert stats.total_files == 2 and stats.files_added == 1, stats

    # per-file patch split
    patches = _split_patches(_DIFF)
    assert set(patches) == {"app.py", "new.py"}, patches.keys()
    assert "+import sys" in patches["app.py"]

    # JSON extraction from fenced + prose-wrapped agent output
    assert _extract_json('here you go:\n```json\n{"a": 1}\n```\nthanks') == {"a": 1}
    assert _extract_json('noise {"b": [1,2], "c": "}"} trailing') == {"b": [1, 2], "c": "}"}
    assert _extract_json('[{"x": 1}]') == [{"x": 1}]

    # scoring: confidence threshold filtering + adversary multiplier
    cfg = ScoringConfig()
    findings = [
        ReviewFinding(dimension_id="d", dimension_name="D", file_path="app.py",
                      line_start=4, line_end=4, severity="critical",
                      title="Real bug", body="b", confidence=0.9),
        ReviewFinding(dimension_id="d", dimension_name="D", file_path="app.py",
                      line_start=2, line_end=2, severity="nitpick",
                      title="Too unsure", body="b", confidence=0.1),  # below nitpick threshold 0.4
    ]
    adv = [AdversaryResult(finding_title="Real bug", verdict="confirmed", reason="r")]
    scored = score_findings(findings, adv, cfg)
    titles = [f.title for f in scored]
    assert "Real bug" in titles and "Too unsure" not in titles, titles
    # confirmed multiplier applied: 1.0 * 0.9 * 1.3
    assert abs(scored[0].score - round(1.0 * 0.9 * 1.3, 3)) < 1e-6, scored[0].score

    # review event from merge-gate blocking flag
    advisory = [ScoredFinding(id="f", dimension_id="d", dimension_name="D",
                              file_path="a", line_start=1, line_end=1,
                              severity="important", title="t", body="b")]
    assert determine_review_event(advisory) == "COMMENT"
    advisory[0].blocking = True
    assert determine_review_event(advisory) == "REQUEST_CHANGES"
    assert determine_review_event([]) == "APPROVE"

    print("review.selftest: all checks passed")


if __name__ == "__main__":
    demo()

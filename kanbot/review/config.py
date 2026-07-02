"""Configuration for the review engine.

All behavioral tuning in one place: budget caps, scoring weights, comment
formatting, depth profiles. (The HITL / opencode / agentfield wiring from the
original engine is dropped — KanBot drives its own CLI agents via runtime.py.)
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .schemas.input import ReviewInput


class BudgetConfig(BaseModel):
    max_duration_seconds: int = 1800
    max_concurrent_reviewers: int = 8
    max_child_spawns_per_reviewer: int = 2
    max_coverage_iterations: int = 2
    max_review_depth: int = 2


class ScoringConfig(BaseModel):
    """Deterministic scoring weights and multipliers.

    LLMs reason about issues; code computes scores. Same findings always produce
    the same scores.
    """

    base_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "critical": 1.0,
            "important": 0.7,
            "suggestion": 0.3,
            "nitpick": 0.1,
        }
    )
    multipliers: dict[str, float] = Field(
        default_factory=lambda: {
            "cross_ref_compound": 1.5,
            "adversary_confirmed": 1.3,
            "adversary_challenged": 0.5,
            "ai_generated_pr": 1.2,
            "blast_radius_high": 1.2,
        }
    )
    confidence_thresholds: dict[str, float] = Field(
        default_factory=lambda: {
            "critical": 0.2,
            "important": 0.3,
            "suggestion": 0.4,
            "nitpick": 0.4,
        }
    )


class CommentConfig(BaseModel):
    min_severity: str = "nitpick"
    max_comments: int = 25
    include_suggestions: bool = True
    # Parallel merge-gate pass: classify each finding blocking vs advisory with a
    # tight release-manager bar. Default ON for noise reduction.
    merge_gate_enabled: bool = True
    severity_emojis: dict[str, str] = Field(
        default_factory=lambda: {
            "critical": "🔴",
            "important": "🟠",
            "suggestion": "🔵",
            "nitpick": "⚪",
        }
    )


class DepthProfile(BaseModel):
    max_dimensions: int = 6
    model_tier: str = "standard"


DEPTH_PROFILES: dict[str, DepthProfile] = {
    "quick": DepthProfile(max_dimensions=3, model_tier="budget"),
    "standard": DepthProfile(max_dimensions=6, model_tier="standard"),
    "deep": DepthProfile(max_dimensions=12, model_tier="premium"),
}

# Auto-depth thresholds (lines changed → depth). <100 quick, 100-500 standard, >500 deep.
AUTO_DEPTH_THRESHOLDS = {100: "quick", 500: "standard"}


class ReviewConfig(BaseModel):
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    comments: CommentConfig = Field(default_factory=CommentConfig)

    ignore_paths: list[str] = Field(
        default_factory=lambda: [
            "*.md", "*.txt", ".github/**", "vendor/**", "node_modules/**",
            "**/*.generated.*", "**/*.min.js", "**/*.min.css",
            "**/package-lock.json", "**/yarn.lock", "**/poetry.lock",
        ]
    )
    hints: list[str] = Field(default_factory=list)

    @classmethod
    def from_input(cls, review_input: ReviewInput) -> ReviewConfig:
        config = cls()
        config.budget.max_duration_seconds = review_input.max_duration_seconds
        if review_input.max_concurrent_reviewers is not None:
            config.budget.max_concurrent_reviewers = review_input.max_concurrent_reviewers
        if review_input.max_coverage_iterations is not None:
            config.budget.max_coverage_iterations = review_input.max_coverage_iterations
        config.budget.max_review_depth = min(review_input.max_review_depth, 3)
        if review_input.ignore_paths:
            config.ignore_paths = list(set(config.ignore_paths + review_input.ignore_paths))
        if review_input.hints:
            config.hints = review_input.hints
        return config

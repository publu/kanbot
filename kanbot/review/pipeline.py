"""The review pipeline — KanBot-native orchestration of the engine phases.

Replaces the original GitHub/HITL/budget-bound orchestrator with the same phase
*sequence*, wired for KanBot's world: input is a local git diff + repo, output is
a markdown review (which KanBot surfaces on a card), not a posted GitHub review.

Phase flow (faithful to the engine):
    intake → anatomy → meta-selectors (3 lenses) → parallel review (+ 1 level of
    sub-reviews) → evidence extraction (AST/grep) → evidence verification →
    parallel adversary → compound-risk → deterministic scoring → merge gate.

# ponytail: dropped vs. original — GitHub API posting, human-approval gating, per-call
# cost/budget accounting (no cost signal from a CLI agent), the multi-iteration
# coverage loop, and recursive sub-reviews past depth 1. Add back when needed.
"""
from __future__ import annotations

import asyncio

from pydantic import BaseModel, Field

from .config import ReviewConfig
from .evidence import extract_evidence_for_findings
from .diff_engine import parse_unified_diff
from .harnesses import (
    adversary_phase,
    anatomy_phase,
    compound_finder_phase,
    evidence_verifier,
    intake_phase,
    meta_mechanical,
    meta_semantic,
    meta_systemic,
    review_dimension,
)
from .merge_gate import classify_findings
from .runtime import router
from .schemas.input import ChangedFile, GitHubPRData
from .schemas.output import ScoredFinding
from .schemas.pipeline import (
    AdversaryResult,
    AnatomyResult,
    MetaDimensionResult,
    ReviewDimension,
    ReviewFinding,
)
from .scoring import determine_review_event, score_findings


def _split_patches(diff_text: str) -> dict[str, str]:
    """Split a unified diff into {path: per-file patch}."""
    patches: dict[str, str] = {}
    path = ""
    buf: list[str] = []

    def flush() -> None:
        if path and buf:
            patches[path] = "\n".join(buf)

    for line in diff_text.splitlines():
        if line.startswith("diff --git"):
            flush()
            buf = [line]
            path = ""
        elif line.startswith("+++ b/"):
            path = line[6:]
            buf.append(line)
        else:
            buf.append(line)
    flush()
    return patches


def build_pr_data(diff_text: str, title: str = "", description: str = "") -> GitHubPRData:
    """Construct the engine's PR input from a local diff."""
    files = parse_unified_diff(diff_text)
    patches = _split_patches(diff_text)
    changed = [
        ChangedFile(
            path=f.path,
            status=f.status,
            additions=f.lines_added,
            deletions=f.lines_removed,
            patch=patches.get(f.path, ""),
        )
        for f in files
    ]
    return GitHubPRData(
        owner="", repo="", number=0,
        title=title or "Local changes",
        description=description,
        diff=diff_text,
        changed_files=changed,
    )


def _collect_dimensions(lenses: list[dict]) -> list[ReviewDimension]:
    """Flatten + dedupe dimensions across the three meta-selector lenses."""
    dims: list[ReviewDimension] = []
    seen: set[str] = set()
    for raw in lenses:
        result = MetaDimensionResult.model_validate(raw)
        for dim in result.dimensions:
            key = (dim.name.strip().lower(), tuple(sorted(dim.target_files)))
            if key in seen:  # type: ignore[comparison-overlap]
                continue
            seen.add(key)  # type: ignore[arg-type]
            dims.append(dim)
    dims.sort(key=lambda d: d.priority, reverse=True)
    return dims


class ReviewOutput:
    def __init__(self, findings: list[ScoredFinding], event: str, markdown: str, meta: dict):
        self.findings = findings
        self.event = event  # APPROVE | COMMENT | REQUEST_CHANGES
        self.markdown = markdown
        self.meta = meta


async def review(
    diff_text: str,
    repo_path: str = "",
    title: str = "",
    description: str = "",
    depth: str = "auto",
    config: ReviewConfig | None = None,
) -> ReviewOutput:
    cfg = config or ReviewConfig()
    pr = build_pr_data(diff_text, title, description)
    pr_dict = pr.model_dump()
    diff_patches = _split_patches(diff_text)

    # Phase 1: intake
    intake = await intake_phase(pr_dict, depth)
    review_depth = intake.get("review_depth", "standard")
    ai_generated = float(intake.get("ai_generated", 0.0) or 0.0)

    # Phase 2: anatomy
    anatomy = await anatomy_phase(pr_dict, intake, repo_path)
    anatomy_obj = AnatomyResult.model_validate(anatomy)
    blast_radius = anatomy_obj.blast_radius
    pr_context = anatomy_obj.pr_narrative or intake.get("pr_summary", "")

    # Phase 3: meta-selectors (3 lenses in parallel) → review dimensions
    lenses = await asyncio.gather(
        meta_semantic(intake, anatomy, review_depth, repo_path, diff_patches),
        meta_mechanical(intake, anatomy, review_depth, repo_path, diff_patches),
        meta_systemic(intake, anatomy, review_depth, repo_path, diff_patches),
    )
    dimensions = _collect_dimensions(list(lenses))[: cfg.budget.max_concurrent_reviewers]

    # Phase 4: parallel review across dimensions
    dim_names = [d.name for d in dimensions]

    async def run_dim(dim: ReviewDimension) -> dict:
        return await review_dimension(
            review_prompt=dim.review_prompt,
            target_files=dim.target_files,
            context_files=dim.context_files,
            repo_path=repo_path,
            current_depth=0,
            max_depth=1,
            pr_narrative=anatomy_obj.pr_narrative,
            risk_surfaces=anatomy_obj.risk_surfaces,
            intake_summary=intake.get("pr_summary", ""),
            diff_patches=diff_patches,
            all_dimension_names=dim_names,
        )

    findings: list[ReviewFinding] = []
    sub_requests: list[dict] = []
    for res in await asyncio.gather(*[run_dim(d) for d in dimensions]):
        for f in res.get("findings", []):
            findings.append(ReviewFinding.model_validate(f))
        sub_requests.extend(res.get("sub_reviews", []))

    # One level of sub-reviews (engine allows recursion; we cap at 1).
    if sub_requests:
        async def run_sub(sr: dict) -> dict:
            return await review_dimension(
                review_prompt=sr["review_prompt"],
                target_files=sr["target_files"],
                context_files=sr.get("context_files", []),
                repo_path=repo_path,
                current_depth=1,
                max_depth=1,
                pr_narrative=anatomy_obj.pr_narrative,
                diff_patches=diff_patches,
                all_dimension_names=dim_names,
            )

        for res in await asyncio.gather(*[run_sub(sr) for sr in sub_requests[:4]]):
            for f in res.get("findings", []):
                findings.append(ReviewFinding.model_validate(f))

    if not findings:
        md = _render_markdown([], "APPROVE", intake, len(dimensions))
        return ReviewOutput([], "APPROVE", md, {"dimensions": len(dimensions), "findings": 0})

    # Phase 5a: evidence extraction (AST/grep, deterministic — no LLM)
    evidence_map = await extract_evidence_for_findings(
        findings, repo_path, diff_patches, blast_radius
    )
    evidence_dict = {title_: pkg.model_dump() for title_, pkg in evidence_map.items()}

    # Phase 5b: independent evidence verification → adjust confidence/severity
    verified = await evidence_verifier(
        [f.model_dump() for f in findings], evidence_dict, pr_context, repo_path
    )
    by_title = {v["title"]: v for v in verified.get("verified_findings", [])}
    kept: list[ReviewFinding] = []
    for f in findings:
        v = by_title.get(f.title)
        if v and not v.get("verified", True) and float(v.get("revised_confidence", 1.0)) < 0.5:
            continue  # verifier refuted it with low confidence — drop
        if v:
            f.confidence = float(v.get("revised_confidence", f.confidence) or f.confidence)
        kept.append(f)
    findings = kept

    # Phase 5c: adversarial challenge (falsifiability)
    adv = await adversary_phase(
        [f.model_dump() for f in findings], ai_generated, pr_context, repo_path, evidence_dict
    )
    adversary_results = [AdversaryResult.model_validate(r) for r in adv.get("results", [])]

    # Phase 5d: compound-risk synthesis over the surviving findings
    if len(findings) >= 2:
        compound = await compound_finder_phase(
            [f.model_dump() for f in findings], repo_path, evidence_dict
        )
        for cf in compound.get("findings", []):
            try:
                findings.append(ReviewFinding.model_validate({
                    "dimension_id": "compound",
                    "dimension_name": "Compound Risk",
                    **{k: cf[k] for k in (
                        "file_path", "line_start", "line_end", "severity",
                        "title", "body", "evidence", "suggestion", "confidence", "tags",
                    ) if k in cf},
                }))
            except Exception:  # noqa: BLE001
                continue

    # Phase 6: deterministic scoring
    scored = score_findings(
        findings, adversary_results, cfg.scoring,
        ai_generated=ai_generated, blast_radius_size=len(blast_radius),
    )

    # Phase 7: merge gate (blocking vs advisory) → review event
    if cfg.comments.merge_gate_enabled and scored:
        scored = await classify_findings(router.app, scored)
    scored = scored[: cfg.comments.max_comments]
    event = determine_review_event(scored)

    md = _render_markdown(scored, event, intake, len(dimensions))
    meta = {
        "dimensions": len(dimensions),
        "findings": len(scored),
        "blocking": sum(1 for f in scored if f.blocking),
        "ai_generated": ai_generated,
        "blast_radius": len(blast_radius),
    }
    return ReviewOutput(scored, event, md, meta)


class _GateFinding(BaseModel):
    severity: str = "important"
    file: str = ""
    line: int = 0
    issue: str = ""


class _GateVerdict(BaseModel):
    blocking: bool = False  # True → REQUEST_CHANGES (the gate rejects the work)
    summary: str = ""
    findings: list[_GateFinding] = Field(default_factory=list)


async def gate_review(diff_text: str, repo_path: str = "", title: str = "") -> ReviewOutput:
    """Fast single-pass review for use as a chain Gate.

    The full `review()` pipeline (3 lenses, parallel reviewers, adversary,
    compound, per-finding merge-gate) is too slow/expensive to run every loop.
    This is the distilled gate: ONE agent pass that reads the repo and returns a
    block/pass verdict + the few findings that justify it. Same engine, one call.
    """
    pr = build_pr_data(diff_text, title)
    files = ", ".join(f.path for f in pr.changed_files[:40]) or "(no files)"
    prompt = (
        "You are a strict merge gate. Decide whether this change can ship as-is.\n\n"
        "Read the actual repository files you need (you have access), not just the diff. "
        "Set blocking=true ONLY for things that must be fixed before merge: a broken build "
        "or tests, a security hole reachable from real input, data loss, a broken public "
        "contract, or a regression of working behavior. Style, naming, missing nice-to-have "
        "tests, and speculative concerns are NOT blocking.\n\n"
        "List only the findings that justify your verdict, each as severity/file/line/issue.\n\n"
        f"## Title\n{title or 'Local changes'}\n\n## Changed files\n{files}\n\n"
        f"## Diff\n```diff\n{diff_text[:12000]}\n```"
    )
    res = await router.app.harness(prompt, schema=_GateVerdict, cwd=repo_path or None)
    verdict = res.parsed or _GateVerdict()
    event = "REQUEST_CHANGES" if verdict.blocking else "APPROVE"
    lines = [f"## Gate: {_EVENT_LABEL.get(event, event)}", ""]
    if verdict.summary:
        lines.append(verdict.summary)
    for f in verdict.findings:
        loc = f" `{f.file}:{f.line}`" if f.file else ""
        lines.append(f"- {_EMOJI.get(f.severity, '•')}{loc} {f.issue}")
    findings = [
        ScoredFinding(
            id=f"g_{i}", dimension_id="gate", dimension_name="Gate",
            file_path=f.file, line_start=f.line, line_end=f.line,
            severity=f.severity, title=f.issue, body="", blocking=verdict.blocking,
        )
        for i, f in enumerate(verdict.findings)
    ]
    return ReviewOutput(findings, event, "\n".join(lines), {"gate": True, "blocking": verdict.blocking})


_EMOJI = {"critical": "🔴", "important": "🟠", "suggestion": "🔵", "nitpick": "⚪"}
_EVENT_LABEL = {
    "REQUEST_CHANGES": "🔴 Changes requested",
    "COMMENT": "🟠 Comments",
    "APPROVE": "🟢 Approved",
}


def _render_markdown(findings: list[ScoredFinding], event: str, intake: dict, dimensions: int) -> str:
    lines = [f"## Review: {_EVENT_LABEL.get(event, event)}", ""]
    pr_type = intake.get("pr_type", "?")
    complexity = intake.get("complexity", "?")
    lines.append(
        f"_{pr_type} · {complexity} · {dimensions} review dimension(s) · "
        f"{len(findings)} finding(s)_"
    )
    if intake.get("pr_summary"):
        lines += ["", intake["pr_summary"]]
    if not findings:
        lines += ["", "No findings. Nothing blocks merge."]
        return "\n".join(lines)

    blocking = [f for f in findings if f.blocking]
    advisory = [f for f in findings if not f.blocking]

    def block(title: str, group: list[ScoredFinding]) -> None:
        if not group:
            return
        lines.append("")
        lines.append(f"### {title}")
        for f in group:
            loc = f"`{f.file_path}:{f.line_start}`" if f.file_path else ""
            lines.append("")
            lines.append(f"{_EMOJI.get(f.severity, '•')} **{f.title}** {loc}")
            if f.blocking and f.blocking_reason:
                lines.append(f"> blocks merge — {f.blocking_reason}")
            if f.body:
                lines.append("")
                lines.append(f.body)
            if f.suggestion:
                lines += ["", "_Suggested fix:_ " + f.suggestion]

    block("Must fix before merge", blocking)
    block("Advisory", advisory)
    return "\n".join(lines)

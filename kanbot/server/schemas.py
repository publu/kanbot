"""Pydantic request bodies for the REST API."""
from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel


class BoardCreate(BaseModel):
    name: str
    repo_path: str = ""


class CardCreate(BaseModel):
    title: str
    prompt: str = ""
    agent: str = "auto"
    cwd: str = ""
    column_id: Optional[str] = None
    loop_max: int = 1
    loop_until: str = ""
    profile: str = ""
    command: str = ""  # optional raw command override (argv template with {prompt})
    plan_mode: bool = False
    plan_auto: bool = False  # plan mode, but auto-approve the plan and run without waiting
    isolate: bool = False    # run in its own git worktree/branch (Devin-style isolation)
    interactive: bool = False  # open the agent's TUI in a live pane instead of headless print mode


class CardPatch(BaseModel):
    title: Optional[str] = None
    prompt: Optional[str] = None
    agent: Optional[str] = None
    cwd: Optional[str] = None
    status: Optional[str] = None
    auto_advance: Optional[bool] = None
    loop_max: Optional[int] = None
    loop_until: Optional[str] = None
    profile: Optional[str] = None
    command: Optional[str] = None
    plan_mode: Optional[bool] = None
    isolate: Optional[bool] = None
    interactive: Optional[bool] = None


class CardMove(BaseModel):
    column_id: str
    position: int = 0


class ProviderKey(BaseModel):
    """Set/clear one provider's API key (empty value clears it)."""
    agent: str
    key: str = ""


class ChainStep(BaseModel):
    """One follow-up step in a "Then…" composer chain. A Step, or a Gate."""
    name: str = ""
    prompt: str = ""
    agent: str = ""           # "" inherits the chain's agent
    command: str = ""         # raw command (a gate's verdict producer, e.g. `kanbot review --gate`)
    loop_max: int = 1         # >1 = Ralph loop this step (e.g. "fix until tests pass")
    loop_until: str = ""      # shell predicate; exit 0 in cwd stops the loop early
    gate: bool = False        # this step is a Gate: on fail, loop back to the prior step
    max_retries: int = 2      # gate: how many times to loop the work back before giving up


class ChainRequest(BaseModel):
    """Run a prompt, then daisy-chain follow-up steps (design, test, review, …).

    Each follow-up sees the previous step's output (carry_context). With no
    follow-ups this is just a normal single card.
    """
    title: str
    prompt: str = ""          # the first/base step the user typed
    agent: str = "auto"
    cwd: str = ""
    profile: str = ""
    steps: List[ChainStep] = []   # follow-up steps, in order
    run: bool = True


class TagCreate(BaseModel):
    name: str
    color: str = "#6b7280"
    insight: str = ""
    config: dict = {}


class TagAttach(BaseModel):
    tag_id: str


class UploadRequest(BaseModel):
    name: str = "image.png"
    data: str  # a data URL, e.g. "data:image/png;base64,...."


class ReviveRequest(BaseModel):
    runner_id: str
    agent: str
    session_id: str
    cwd: str = ""
    title: str = ""
    prompt: str = ""
    run: bool = True
    interactive: bool = True   # resume in the agent's TUI (live, typeable) by default


# -- workflows ------------------------------------------------------------
class WorkflowStep(BaseModel):
    name: str = ""
    prompt: str = ""
    agent: str = ""           # "" inherits the workflow's agent
    profile: str = ""
    command: str = ""
    loop_max: int = 1
    loop_until: str = ""
    carry_context: bool = True
    continue_on_fail: bool = False
    gate: bool = False        # Gate step: on fail, loop back to the prior step
    max_retries: int = 2      # gate: loop-back budget before giving up


class WorkflowSave(BaseModel):
    """Create or replace a workflow + its steps. Doubles as the import body."""
    name: str
    description: str = ""
    agent: str = "auto"
    cwd: str = ""
    steps: List[WorkflowStep] = []


class WorkflowImport(BaseModel):
    template: dict   # a workflow_template() dict (name, agent, cwd, steps[])


class WorkflowRun(BaseModel):
    cwd: str = ""        # override the workflow's default cwd for this run
    title: str = ""      # override the run card title
    run: bool = True     # dispatch immediately (vs. park in backlog)


class WorkflowExtract(BaseModel):
    session_id: str = ""           # a single session to extract from
    session_ids: List[str] = []    # several sessions merged (in this order) into one extraction
    split: bool = True             # segment by topic into multiple workflows (vs. one combined)
    save: bool = False             # persist the result (vs. return a preview to edit first)


class WorkflowClone(BaseModel):
    name: str = ""


class FromSession(BaseModel):
    session_id: str = ""
    session_ids: List[str] = []
    refresh: bool = False        # ignore the cache and re-extract


class WorkflowEval(BaseModel):
    template: dict               # the workflow template to judge
    session_id: str = ""         # the session it was distilled from (ground truth)
    keep: bool = True            # save as an exemplar if it scores above the bar
    sandbox: bool = False        # Phase B: replay in a worktree + judge the real diff


class ImproveRequest(BaseModel):
    limit: int = 2               # how many sessions to run in this pass (cost cap)
    sandbox: bool = False        # Phase B execution-grounded reward (slow)


class BuildRequest(BaseModel):
    session_ids: List[str] = []  # focus sessions to auto-analyze + stream


class DraftRequest(BaseModel):
    """Author a brand-new playbook from a freeform description (not a session)."""
    description: str
    cwd: str = ""                # optional repo to ground the steps in (read-only)


class SpreeRequest(BaseModel):
    """Set off a long, unattended 'goal spree' — decompose + grind + verify."""
    goal: str                    # the whole objective to drive to completion
    cwd: str = ""                # repo to work in (where PROGRESS.md lands)
    verify_cmd: str = ""         # optional shell predicate that must pass to finish
    hours: float = 10.0          # wall-clock budget
    loop_max: int = 200          # max fresh-context grind iterations
    profile: str = ""            # prompt mode (e.g. 'lean') applied to every step
    playbook_id: str = ""        # optional saved playbook whose method seeds the run
    title: str = ""              # run card title (defaults from the goal)
    run: bool = True             # dispatch now (vs park in backlog)


# -- panes (runner-owned terminals) -----------------------------------------
class PaneStart(BaseModel):
    agent: str = "claude"
    prompt: str = ""
    cwd: str = ""
    interactive: bool = True
    resume: str = ""
    title: str = ""
    runner_id: str = ""        # "" = any online runner that has the agent


class PaneInput(BaseModel):
    text: str = ""             # typed text (Enter appended unless enter=false)
    enter: bool = True
    keys: Optional[List[str]] = None   # tmux-style key names instead of text: ["y", "Enter"]

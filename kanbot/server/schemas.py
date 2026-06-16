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


class CardMove(BaseModel):
    column_id: str
    position: int = 0


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
    session_id: str      # a discovered agent session to extract a workflow from
    save: bool = True    # persist the extracted draft (vs. just return it)


class WorkflowClone(BaseModel):
    name: str = ""

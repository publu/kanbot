"""Pydantic request bodies for the REST API."""
from __future__ import annotations

from typing import Optional

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


class CardPatch(BaseModel):
    title: Optional[str] = None
    prompt: Optional[str] = None
    agent: Optional[str] = None
    cwd: Optional[str] = None
    status: Optional[str] = None
    auto_advance: Optional[bool] = None


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


class ReviveRequest(BaseModel):
    runner_id: str
    agent: str
    session_id: str
    cwd: str = ""
    title: str = ""
    prompt: str = ""
    run: bool = True

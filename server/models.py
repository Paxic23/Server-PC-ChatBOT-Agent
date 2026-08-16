"""Pydantic request/response models for the HTTP API."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class CreateSessionRequest(BaseModel):
    network_mode: Optional[str] = None


class CreateSessionResponse(BaseModel):
    session_id: str
    network_mode: str
    created_at: str


class UploadResponse(BaseModel):
    extracted: list[str]


class RejectToolRequest(BaseModel):
    reason: Optional[str] = None


class PendingToolSummary(BaseModel):
    id: str
    name: str
    description: str
    proposed_at: str
    session_id: str


class PendingToolDetail(PendingToolSummary):
    input_schema: dict
    code: str

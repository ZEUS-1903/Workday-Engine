"""Request bodies. FastAPI uses these for validation and for the /docs page."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
Salary = Field(gt=0, le=10_000_000, description="Annual base salary in USD")


class HireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=2, max_length=120)
    email: str = Field(pattern=EMAIL_PATTERN, max_length=200)
    job_profile_id: str
    org_id: str = Field(description="Supervisory organization the new hire joins")
    salary: int = Salary
    effective_date: date = Field(description="Hire date")


class ChangeJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str
    effective_date: date
    reason: Literal["Promotion", "Transfer", "Merit Increase", "Market Adjustment", "Other"]
    job_profile_id: str | None = None
    org_id: str | None = None
    salary: int | None = Field(default=None, gt=0, le=10_000_000)


class TerminationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str
    effective_date: date = Field(description="Termination date")
    reason: Literal["Resignation", "Retirement", "Layoff", "Performance", "Other"]


class CommentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=2000)


class SendBackBody(CommentBody):
    to_step_id: int


class ResubmitData(BaseModel):
    """Fields an initiator may change when a step is sent back to them."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=120)
    email: str | None = Field(default=None, pattern=EMAIL_PATTERN, max_length=200)
    job_profile_id: str | None = None
    org_id: str | None = None
    salary: int | None = Field(default=None, gt=0, le=10_000_000)
    reason: str | None = Field(default=None, max_length=60)
    effective_date: date | None = None


class SubmitBody(CommentBody):
    data: ResubmitData | None = None

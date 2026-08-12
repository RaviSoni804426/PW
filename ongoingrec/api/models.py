"""Request and response shapes for the local API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..timeutil import parse_timestamp


class FetchRequest(BaseModel):
    """A clip request (PRD sections 12 to 15).

    Validation follows section 15 exactly: a timestamp is always required,
    and at least one identifier must be present. The rules are enforced here,
    at the edge, so the handler only ever sees a request that makes sense.
    """

    email_id: str | None = None
    employee_id: str | None = None
    timestamp: str
    window_seconds: int | None = Field(default=None, ge=1, le=7200)

    @model_validator(mode="after")
    def check_identity_and_time(self) -> "FetchRequest":
        if not (self.email_id or self.employee_id):
            # Section 15: a timestamp alone cannot identify an installation.
            raise ValueError("at least one of email_id or employee_id is required")
        try:
            parse_timestamp(self.timestamp)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self

    @property
    def requested_at(self):
        return parse_timestamp(self.timestamp)


class HealthResponse(BaseModel):
    """PRD section 20, plus what an operator actually needs to diagnose a
    laptop that is running but not producing audio."""

    status: str
    version: str
    install_id: str
    email_id: str
    employee_id: str
    recording: bool
    device: str | None = None
    current_segment_start: str | None = None
    last_segment_end: str | None = None
    segment_count: int = 0
    free_disk_mb: float = 0.0
    registered: bool = False
    local_time_offset: str = "+00:00"
    detail: dict[str, Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None

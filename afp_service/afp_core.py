# -*- coding: utf-8 -*-
"""AFP L1 core models (protocol doc 02): Entity / AFPError / PageResult.

Field-for-field aligned with 02-L1-Entity-Model.md:
- Entity required: entity_id / domain / payload / meta(trace_id, timestamp)
- sha256: null when no blob; empty string forbidden (02 section 1.3)
- error_code: controlled enum of exactly 8 values (02 section 3.2)
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ErrorCode(StrEnum):
    """Controlled error_code enum (02 section 3.2, 8 values)."""

    OK = "ok"
    ENTITY_MISSING = "entity_missing"
    PARTIAL_MATCH = "partial_match"
    RATE_LIMIT = "rate_limit"
    INVALID_PARAM = "invalid_param"
    FETCH_REMOTE_FAILED = "fetch_remote_failed"
    AUTH_FAILED = "auth_failed"
    CAPABILITY_NOT_SUPPORTED = "capability_not_supported"


# error_code -> HTTP status (doc 02 section 3.2 / doc 06 section 3.1)
ERROR_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.OK: 200,
    ErrorCode.ENTITY_MISSING: 404,
    ErrorCode.PARTIAL_MATCH: 200,
    ErrorCode.RATE_LIMIT: 429,
    ErrorCode.INVALID_PARAM: 400,
    ErrorCode.FETCH_REMOTE_FAILED: 502,
    ErrorCode.AUTH_FAILED: 401,
    ErrorCode.CAPABILITY_NOT_SUPPORTED: 501,
}


class Meta(BaseModel):
    trace_id: str
    timestamp: str = Field(default_factory=utc_now)


class Entity(BaseModel):
    """AFP entity object (protocol L1)."""

    entity_id: str
    domain: str
    revision: Optional[str] = None
    source_origin: Optional[str] = None
    sha256: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    meta: Meta

    @field_validator("sha256")
    @classmethod
    def _sha256_null_or_hash(cls, v: Optional[str]) -> Optional[str]:
        # 02 section 1.3: no attachment => null; empty string is forbidden
        if v is not None and not v.strip():
            raise ValueError(
                "sha256 must be null when no blob is attached; empty string is forbidden"
            )
        return v


class AFPError(BaseModel):
    """AFP unified error object (protocol L1)."""

    error_code: ErrorCode
    human_readable: str = ""
    warnings: list[str] = Field(default_factory=list)
    trace_id: str
    retry_after_sec: int = 0


class PageResult(BaseModel):
    items: list[Entity]
    total: int
    offset: int
    limit: int
    has_more: bool

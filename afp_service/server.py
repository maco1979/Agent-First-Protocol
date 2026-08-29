# -*- coding: utf-8 -*-
"""AFP HTTP-Web Binding (doc 06): M1 minimal + M2 full compliance.

Fixes over the doc 11 reference implementation (M1):
1. lookup miss => HTTP 404 + AFPError (was 200-wrapped; hard constraint 8)
2. blob miss   => HTTP 404 + AFPError (was 200-wrapped)
3. no `request: Request = None` anti-pattern; FastAPI 422 normalized to
   400 + AFPError(invalid_param)
Additional spec decisions applied here:
- partial-match: source_origin = null (spec P-2, no status-marker misuse)
- unknown domain => 404 + entity_missing "domain not served" (ADR-I2)
- blob Accept: application/json => locator set (ADR-I1, doc 04 section 2.4)
- three-tier Cache-Control (doc 06 section 6): discover 300 / entity 3600 /
  blob immutable 31536000

M2 full-compliance increments (doc 06 sections 5/7, doc 10 section 2):
- list: offset >= 0, limit default 20 max 100, abuse => 400 invalid_param;
  responses are PageResult
- rate limiting: sliding-window middleware => 429 + AFPError(rate_limit,
  retry_after_sec); env switch exists (an intranet instance may disable
  the limiter, but the configuration item itself must exist, doc 06 section 7)
- audit log: JSON lines with trace_id / caller / query key / duration /
  fetch_triggered (always false, reserved for fetch_external) /
  fetch_duration_ms (always 0, reserved)
- api_key auth behind an env switch: X-AFP-Api-Key header, failure =>
  401 + AFPError(auth_failed); the discover auth declaration always
  mirrors the live mode (none when disabled)
- multi-revision store: same part_number keeps several revisions; lookup
  ?revision= picks the exact one, null picks the newest

M4 note: build_discovery() / is_valid_sha256() are module-level so the MCP
binding (mcp_binding.py) renders the exact same discovery document and the
same sha256 validation as this HTTP binding.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response

from afp_core import (
    AFPError,
    Entity,
    ErrorCode,
    ERROR_HTTP_STATUS,
    Meta,
    PageResult,
    utc_now,
)
from store import ComponentStore

ROOT = Path(__file__).resolve().parent.parent

SERVED_DOMAINS = ["component"]

# capabilities: only what is actually implemented here (no remote_fetch)
CAPABILITIES = ["entity_lookup", "entity_list", "blob_access"]

PARTIAL_WARNING = (
    "partial-match: key is not an exact entity id; candidates are in payload.candidates; "
    "do not use silently for production generation, verify against the datasheet"
)

_TRUTHY = {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Runtime configuration for one service instance (doc 06 sections 5/7).

    Every knob exists even when disabled: intranet instances may turn rate
    limiting or auth off, but the configuration items are part of the contract.
    """

    db_path: str = str(ROOT / "afp.db")
    blob_dir: str = str(ROOT / "data")
    auth_mode: str = "none"  # none | api_key (doc 06 section 5.1)
    api_key: str = ""
    rate_limit_enabled: bool = False
    rate_limit_max_req: int = 60
    rate_limit_window_sec: float = 60.0
    audit_log_path: str = str(ROOT / "logs" / "afp_audit.jsonl")

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            db_path=os.environ.get("AFP_DB", str(ROOT / "afp.db")),
            blob_dir=os.environ.get("AFP_BLOB_DIR", str(ROOT / "data")),
            auth_mode=os.environ.get("AFP_AUTH_MODE", "none"),
            api_key=os.environ.get("AFP_API_KEY", ""),
            rate_limit_enabled=os.environ.get("AFP_RATE_LIMIT_ENABLED", "0").strip().lower() in _TRUTHY,
            rate_limit_max_req=int(os.environ.get("AFP_RATE_LIMIT_MAX_REQ", "60")),
            rate_limit_window_sec=float(os.environ.get("AFP_RATE_LIMIT_WINDOW_SEC", "60")),
            audit_log_path=os.environ.get("AFP_AUDIT_LOG", str(ROOT / "logs" / "afp_audit.jsonl")),
        )


class SlidingWindowLimiter:
    """Instance-wide sliding-window rate limiter (doc 06 section 7).

    One bucket per service instance: the protocol demands 429 +
    retry_after_sec semantics, not per-client buckets.
    """

    def __init__(self, max_req: int, window_sec: float):
        self.max_req = max_req
        self.window_sec = window_sec
        self._hits: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> tuple[bool, int]:
        """Consume one slot; returns (allowed, retry_after_sec)."""
        now = time.monotonic()
        with self._lock:
            while self._hits and now - self._hits[0] >= self.window_sec:
                self._hits.popleft()
            if len(self._hits) >= self.max_req:
                retry_after = self.window_sec - (now - self._hits[0])
                return False, max(1, math.ceil(retry_after))
            self._hits.append(now)
            return True, 0


def resolve_trace(request: Request) -> str:
    # the audit middleware seeds request.state.trace_id once per request so
    # every layer (auth, rate limit, handlers, endpoints) shares one trace_id
    seeded = getattr(request.state, "trace_id", None)
    if seeded:
        return seeded
    return request.headers.get("x-afp-trace-id") or f"trace-{uuid.uuid4().hex[:12]}"


def afp_error_response(err: AFPError) -> JSONResponse:
    return JSONResponse(
        status_code=ERROR_HTTP_STATUS[err.error_code],
        content=err.model_dump(mode="json"),
        headers={"Cache-Control": "no-store"},
    )


def entity_response(entity: Entity) -> JSONResponse:
    return JSONResponse(
        content=entity.model_dump(mode="json"),
        headers={"Cache-Control": "public, max-age=3600"},
    )


def _caller_of(request: Request) -> str:
    key = request.headers.get("x-afp-api-key")
    if key:
        # never log the full key: audit trails must not leak credentials
        return f"api_key:{key[:4]}***"
    if request.client is not None:
        return request.client.host
    return "unknown"


def _query_key_of(request: Request) -> Optional[str]:
    key = request.query_params.get("key")
    if key:
        return key
    if request.url.path.startswith("/api/v1/blob/"):
        return request.url.path.rsplit("/", 1)[-1]
    return None


def _audit_write(path_str: str, record: dict[str, Any]) -> None:
    path = Path(path_str)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def build_discovery(settings: Settings) -> dict[str, Any]:
    """Agent-protocol discovery metadata (L2, doc 03).

    Single source of truth for every binding: the HTTP discover endpoint,
    the MCP afp_discover tool and the IPC discover op all render this same
    document, so the bindings can never drift apart.
    """
    return {
        "protocol": "Agent-First-Protocol",
        "protocol_version": "v0.2",
        "service_name": "Component-Datasheet-Service",
        "capabilities": CAPABILITIES,
        "domains": SERVED_DOMAINS,
        "auth": {"mode": settings.auth_mode},  # mirrors the live mode (doc 06 section 5.1)
        "blob_access_methods": [
            {"method": "http", "template": "/api/v1/blob/{sha256}"},
            {"method": "local_file", "template": "data/{sha256}.bin"},
        ],
        "human_view": {"available": True, "transport": "http", "prefix": "/browser"},
        "extensions": {"service_owner": "zhuhai-lab"},
    }


def is_valid_sha256(s: str) -> bool:
    """64 hex chars — the content-address key format shared by all bindings."""
    return len(s) == 64 and all(ch in "0123456789abcdef" for ch in s.lower())


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    """Build an AFP HTTP binding instance.

    The module-level `app` below uses Settings.from_env(); tests build
    isolated instances with their own databases and feature switches.
    """
    if settings is None:
        settings = Settings.from_env()
    if settings.auth_mode not in ("none", "api_key"):
        # never over-declare: oauth2_bearer exists in doc 06 but is not
        # implemented here, so creating such an app must fail loudly
        raise ValueError(
            f"unsupported auth mode {settings.auth_mode!r}: implemented modes are none, api_key"
        )

    store = ComponentStore(db_path=settings.db_path)
    blob_dir = Path(settings.blob_dir)
    limiter = (
        SlidingWindowLimiter(settings.rate_limit_max_req, settings.rate_limit_window_sec)
        if settings.rate_limit_enabled
        else None
    )

    app = FastAPI(title="AFP Component Service", version="v0.2")

    @app.exception_handler(RequestValidationError)
    async def _validation_to_afp(request: Request, exc: RequestValidationError) -> JSONResponse:
        # doc 06 section 3.1: invalid params => 400 + invalid_param (422 normalized)
        locs = [str(e.get("loc", [])[-1]) for e in exc.errors()]
        return afp_error_response(
            AFPError(
                error_code=ErrorCode.INVALID_PARAM,
                human_readable=f"invalid or missing parameter(s): {', '.join(locs)}",
                trace_id=resolve_trace(request),
            )
        )

    # middleware registration order == onion layering: auth (innermost),
    # rate limit, audit (outermost => records even 429/401 responses)
    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        # doc 06 section 5: auth.mode=api_key => X-AFP-Api-Key on /api/**;
        # discover / llms.txt / human pages stay public so agents can bootstrap
        if settings.auth_mode == "api_key" and request.url.path.startswith("/api/"):
            supplied = request.headers.get("x-afp-api-key", "")
            if supplied != settings.api_key:
                return afp_error_response(
                    AFPError(
                        error_code=ErrorCode.AUTH_FAILED,
                        human_readable="invalid or missing API key (X-AFP-Api-Key header)",
                        trace_id=resolve_trace(request),
                    )
                )
        return await call_next(request)

    @app.middleware("http")
    async def rate_limit_middleware(request: Request, call_next):
        if limiter is None:  # intranet default: off, but the knob exists
            return await call_next(request)
        allowed, retry_after = limiter.acquire()
        if not allowed:
            return afp_error_response(
                AFPError(
                    error_code=ErrorCode.RATE_LIMIT,
                    human_readable=(
                        f"rate limit exceeded: {settings.rate_limit_max_req} requests"
                        f" per {int(settings.rate_limit_window_sec)}s window"
                    ),
                    trace_id=resolve_trace(request),
                    retry_after_sec=retry_after,
                )
            )
        return await call_next(request)

    @app.middleware("http")
    async def audit_middleware(request: Request, call_next):
        # doc 06 section 7: record trace_id / caller / query key / duration /
        # fetch_triggered (reserved, false until fetch_external lands) /
        # fetch_duration_ms (reserved, 0)
        request.state.trace_id = (
            request.headers.get("x-afp-trace-id") or f"trace-{uuid.uuid4().hex[:12]}"
        )
        started = time.perf_counter()
        response = await call_next(request)
        record = {
            "timestamp": utc_now(),
            "trace_id": request.state.trace_id,
            "caller": _caller_of(request),
            "method": request.method,
            "path": request.url.path,
            "query_key": _query_key_of(request),
            "status_code": response.status_code,
            "duration_ms": round((time.perf_counter() - started) * 1000, 3),
            "fetch_triggered": False,
            "fetch_duration_ms": 0,
        }
        if settings.audit_log_path:
            try:
                _audit_write(settings.audit_log_path, record)
            except OSError:
                pass  # a broken audit sink must not take the API down
        return response

    @app.get("/.well-known/agent-protocol.json")
    def discover() -> JSONResponse:
        return JSONResponse(
            content=build_discovery(settings),
            headers={"Cache-Control": "public, max-age=300"},
        )

    @app.get("/llms.txt")
    def llms_txt() -> Response:
        lines = ["# Component Datasheet Service", "", "## Entity Index"]
        for r in store.list_index():
            lines.append(f"- {r['part_number']} | {r['manufacturer']} | {r['category']} | {r['package']}")
        lines += ["", "## Query Hints", "Use entity_lookup with part number.", ""]
        return Response("\n".join(lines), media_type="text/plain")

    @app.get("/api/v1/{domain}/lookup")
    def lookup(
        request: Request,
        domain: str,
        key: str = Query(...),
        revision: Optional[str] = Query(None),
    ) -> JSONResponse:
        trace = resolve_trace(request)
        if domain not in SERVED_DOMAINS:
            # ADR-I2: unserved domain -> 404 entity_missing
            return afp_error_response(
                AFPError(
                    error_code=ErrorCode.ENTITY_MISSING,
                    human_readable=f"domain '{domain}' not served by this endpoint",
                    trace_id=trace,
                )
            )
        row = store.lookup_exact(key, revision)
        if row is not None:
            payload = json.loads(row["json_payload"])
            return entity_response(
                Entity(
                    entity_id=row["part_number"],
                    domain=domain,
                    revision=row["revision"] or payload.get("revision"),
                    source_origin=row.get("source_file") or "unknown",
                    sha256=row.get("datasheet_sha256"),
                    payload=payload,
                    meta=Meta(trace_id=trace),
                )
            )
        if revision is not None:
            # exact-revision miss is a plain miss: it must not degrade
            # into a partial-match candidate list
            return afp_error_response(
                AFPError(
                    error_code=ErrorCode.ENTITY_MISSING,
                    human_readable=f"No entity {key} revision {revision} in domain {domain}",
                    trace_id=trace,
                )
            )
        cands = store.lookup_fuzzy(key)
        if cands:
            return entity_response(
                Entity(
                    entity_id=key,
                    domain=domain,
                    revision=None,
                    source_origin=None,  # P-2: no status-marker misuse
                    sha256=None,
                    payload={"candidates": [c["part_number"] for c in cands]},
                    warnings=[PARTIAL_WARNING],
                    meta=Meta(trace_id=trace),
                )
            )
        return afp_error_response(
            AFPError(
                error_code=ErrorCode.ENTITY_MISSING,
                human_readable=f"No entity {key} in domain {domain}",
                trace_id=trace,
            )
        )

    @app.get("/api/v1/{domain}/list")
    def list_entities(
        request: Request,
        domain: str,
        offset: int = Query(0, ge=0),
        limit: int = Query(20, ge=1, le=100),
    ) -> JSONResponse:
        trace = resolve_trace(request)
        if domain not in SERVED_DOMAINS:
            return afp_error_response(
                AFPError(
                    error_code=ErrorCode.ENTITY_MISSING,
                    human_readable=f"domain '{domain}' not served by this endpoint",
                    trace_id=trace,
                )
            )
        rows, total = store.list_page(offset, limit)
        items = [
            Entity(
                entity_id=r["part_number"],
                domain=domain,
                revision=r.get("revision"),
                source_origin=r.get("source_file") or "unknown",
                sha256=r.get("datasheet_sha256"),
                payload={},
                meta=Meta(trace_id=trace),
            )
            for r in rows
        ]
        result = PageResult(
            items=items,
            total=total,
            offset=offset,
            limit=limit,
            has_more=(offset + limit) < total,
        )
        return JSONResponse(
            content=result.model_dump(mode="json"),
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.get("/api/v1/blob/{sha256}")
    def blob(request: Request, sha256: str) -> Response:
        trace = resolve_trace(request)
        if not is_valid_sha256(sha256):
            return afp_error_response(
                AFPError(
                    error_code=ErrorCode.INVALID_PARAM,
                    human_readable=f"invalid sha256 path segment: {sha256!r}",
                    trace_id=trace,
                )
            )
        path = blob_dir / f"{sha256}.bin"
        if not path.is_file():
            return afp_error_response(
                AFPError(
                    error_code=ErrorCode.ENTITY_MISSING,
                    human_readable=f"blob {sha256} not found",
                    trace_id=trace,
                )
            )
        data = path.read_bytes()
        mime = "application/pdf" if data.startswith(b"%PDF-") else "application/octet-stream"
        if "application/json" in request.headers.get("accept", ""):
            # ADR-I1: content negotiation returns the locator set (doc 04 section 2.4)
            locator = {
                "sha256": sha256,
                "mime_type": mime,
                "size_bytes": len(data),
                "locators": [
                    {"method": "http", "url": f"/api/v1/blob/{sha256}"},
                    {"method": "local_file", "path": f"data/{sha256}.bin"},
                ],
            }
            return JSONResponse(
                content=locator,
                headers={"Cache-Control": "public, max-age=3600"},
            )
        return Response(
            content=data,
            media_type=mime,
            headers={
                "X-Blob-Sha256": sha256,
                "Cache-Control": "public, immutable, max-age=31536000",
            },
        )

    @app.get("/browser/component/{key}")
    def human_view(key: str) -> HTMLResponse:
        # doc 06 section 8: human pages live under /browser/**; agents must not
        # parse business data from this channel
        return HTMLResponse(
            f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{key}</title></head>
<body>
  <h1>Component {key}</h1>
  <p>human view only; agents must use /api/v1/component/lookup</p>
</body></html>"""
        )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=47100)

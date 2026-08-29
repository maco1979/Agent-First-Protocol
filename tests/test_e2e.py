# -*- coding: utf-8 -*-
"""M3 end-to-end acceptance (Task 3.2).

A REAL uvicorn server (127.0.0.1, port 47100 when free, else a random
free port) runs the full agent chain: discover -> lookup found / miss /
partial-match -> blob download with sha256 re-verification. Mock AFP
endpoints (also real uvicorn servers) assert the agent-side refusals of
doc 03 section 6: protocol mismatch and missing capabilities must stop
the client before any business call is issued.
"""
from __future__ import annotations

import hashlib
import socket
import threading
import time
import uuid
from typing import Any

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from client import (
    AFPClient,
    BlobIntegrityError,
    CapabilityNotSupported,
    EntityMissing,
    LookupResult,
    PartialMatchError,
    ProtocolMismatch,
    RemoteAFPError,
)


# ---------------------------------------------------------------------------
# helpers: real uvicorn servers on daemon threads, always torn down
# ---------------------------------------------------------------------------

def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveServer:
    """uvicorn server on a daemon thread with guaranteed teardown."""

    def __init__(self, app: Any, port: int):
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", access_log=False
        )
        self.server = uvicorn.Server(config)
        self.port = port
        self._thread = threading.Thread(target=self.server.run, daemon=True)

    def start(self, timeout: float = 10.0) -> "LiveServer":
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.2)
                if s.connect_ex(("127.0.0.1", self.port)) == 0:
                    return self
            time.sleep(0.05)
        self.stop()
        raise RuntimeError(f"server on port {self.port} did not start within {timeout}s")

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.server.should_exit = True
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# mock AFP endpoint factory for refusal assertions (doc 03 section 6)
# ---------------------------------------------------------------------------

def make_mock_app(
    *,
    protocol: str = "Agent-First-Protocol",
    capabilities: tuple[str, ...] = ("entity_lookup", "blob_access"),
    blob_methods: list[dict] | None = None,
    blob_body: bytes = b"",
    lookup_response: tuple[int, dict] | None = None,
) -> tuple[FastAPI, dict]:
    """Configurable not-quite-AFP endpoint for refusal assertions.

    `counters["paths"]` records every request path, so tests can prove
    the client made — or did NOT make — specific calls.
    """
    counters: dict[str, list[str]] = {"paths": []}
    if blob_methods is None:
        blob_methods = [{"method": "http", "template": "/api/v1/blob/{sha256}"}]
    app = FastAPI()

    @app.get("/.well-known/agent-protocol.json")
    def discover(request: Request) -> JSONResponse:
        counters["paths"].append(request.url.path)
        return JSONResponse(
            {
                "protocol": protocol,
                "protocol_version": "v0.2",
                "service_name": "mock-afp-endpoint",
                "capabilities": list(capabilities),
                "domains": ["component"],
                "blob_access_methods": blob_methods,
            }
        )

    @app.get("/api/v1/{domain}/lookup")
    def lookup(request: Request, domain: str, key: str, revision: str | None = None) -> Response:
        counters["paths"].append(request.url.path)
        if lookup_response is not None:
            status, body = lookup_response
            return JSONResponse(body, status_code=status)
        return JSONResponse(
            {
                "entity_id": key,
                "domain": domain,
                "revision": revision,
                "source_origin": "mock",
                "sha256": None,
                "payload": {"mock": True},
                "warnings": [],
                "meta": {"trace_id": "mock-trace", "timestamp": "2026-01-01T00:00:00Z"},
            }
        )

    @app.get("/api/v1/blob/{sha256}")
    @app.get("/files/{sha256}")
    def blob(request: Request, sha256: str) -> Response:
        counters["paths"].append(request.url.path)
        return Response(
            content=blob_body,
            media_type="application/octet-stream",
            headers={"X-Blob-Sha256": hashlib.sha256(blob_body).hexdigest()},
        )

    return app, counters


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_server():
    """The real AFP service behind a live uvicorn HTTP server."""
    from server import app  # conftest already seeded the store

    port = 47100 if _port_free(47100) else _free_port()
    srv = LiveServer(app, port).start()
    yield srv
    srv.stop()


@pytest.fixture()
def api(real_server) -> AFPClient:
    with AFPClient(real_server.url) as client:
        yield client


@pytest.fixture()
def mock_endpoint():
    started: list[LiveServer] = []

    def _launch(**kwargs) -> tuple[LiveServer, dict]:
        app, counters = make_mock_app(**kwargs)
        srv = LiveServer(app, _free_port()).start()
        started.append(srv)
        return srv, counters

    yield _launch
    for srv in started:
        srv.stop()


# ---------------------------------------------------------------------------
# full chain against the real service
# ---------------------------------------------------------------------------

class TestFullChainOnRealServer:
    def test_discover_validates_protocol_and_caches(self, api):
        meta = api.discover()
        assert meta["protocol"] == "Agent-First-Protocol"
        assert "entity_lookup" in meta["capabilities"]
        assert "blob_access" in meta["capabilities"]
        assert "remote_fetch" not in meta["capabilities"]
        assert "component" in meta["domains"]
        assert api.discover() is meta  # cached: no second HTTP roundtrip

    def test_business_call_lazy_discovers(self, real_server):
        with AFPClient(real_server.url) as client:
            assert client.metadata is None
            res = client.lookup("component", "ADS1299")  # triggers discover()
            assert res.entity_id == "ADS1299"
            assert client.metadata is not None
            assert client.metadata["protocol"] == "Agent-First-Protocol"

    def test_lookup_found(self, api):
        res = api.lookup("component", "MAX30011")
        assert isinstance(res, LookupResult)
        assert res.entity_id == "MAX30011"
        assert res.revision == "Rev-C"
        assert res.source_origin == "ADI official datasheet"
        assert res.sha256 and len(res.sha256) == 64
        assert res.payload["part_number"] == "MAX30011"
        assert res.warnings == []
        assert res.is_partial is False
        assert res.require_exact() is res  # exact match passes the guard

    def test_lookup_miss_raises_entity_missing_with_afp_error_info(self, api):
        with pytest.raises(EntityMissing) as excinfo:
            api.lookup("component", "FAKE_CHIP_999")
        err = excinfo.value
        assert err.error_code == "entity_missing"  # AFPError fields carried
        assert "FAKE_CHIP_999" in err.human_readable
        assert err.trace_id

    def test_lookup_partial_match_forces_warning_exposure(self, api):
        res = api.lookup("component", "MAX3001")
        assert res.is_partial is True
        assert res.warnings  # non-empty: forced exposure on the result type
        assert "MAX30011" in res.payload["candidates"]
        with pytest.raises(PartialMatchError):  # silent use forbidden
            res.require_exact()

    def test_lookup_revision_exact_and_miss(self, api):
        res = api.lookup("component", "MAX30011", revision="Rev-C")
        assert res.revision == "Rev-C"
        with pytest.raises(EntityMissing):
            api.lookup("component", "MAX30011", revision="Rev-Z")

    def test_trace_id_auto_attached_and_unique_per_request(self, api):
        r1 = api.lookup("component", "ADS1299")
        r2 = api.lookup("component", "OPA2340")
        uuid.UUID(r1.trace_id)  # server echoes the client-generated uuid back
        uuid.UUID(r2.trace_id)
        assert r1.trace_id != r2.trace_id

    def test_unserved_domain_raises_entity_missing(self, api):
        with pytest.raises(EntityMissing) as excinfo:
            api.lookup("doc", "whatever")
        assert excinfo.value.error_code == "entity_missing"

    def test_blob_download_verifies_sha256(self, api):
        res = api.lookup("component", "MAX30011")
        data = api.fetch_blob(res.sha256)
        assert data.startswith(b"%PDF-")
        assert hashlib.sha256(data).hexdigest() == res.sha256

    def test_blob_missing_raises_entity_missing(self, api):
        with pytest.raises(EntityMissing) as excinfo:
            api.fetch_blob("0" * 64)
        assert excinfo.value.error_code == "entity_missing"

    def test_blob_invalid_hash_rejected_before_request(self, api):
        with pytest.raises(ValueError):
            api.fetch_blob("not-a-hash")


# ---------------------------------------------------------------------------
# agent-side refusals against mock endpoints (doc 03 section 6)
# ---------------------------------------------------------------------------

class TestAgentSideRefusals:
    def test_protocol_mismatch_refuses_and_makes_no_further_calls(self, mock_endpoint):
        srv, counters = mock_endpoint(protocol="Some-Other-Protocol")
        with AFPClient(srv.url) as client:
            with pytest.raises(ProtocolMismatch):
                client.discover()
            # the refusal is remembered: no further call ever hits the wire
            with pytest.raises(ProtocolMismatch):
                client.lookup("component", "MAX30011")
            with pytest.raises(ProtocolMismatch):
                client.fetch_blob("a" * 64)
        assert counters["paths"] == ["/.well-known/agent-protocol.json"]

    def test_missing_entity_lookup_capability_stops_before_request(self, mock_endpoint):
        srv, counters = mock_endpoint(capabilities=("entity_list",))
        with AFPClient(srv.url) as client:
            meta = client.discover()  # discovery itself is fine: protocol matches
            assert meta["capabilities"] == ["entity_list"]
            with pytest.raises(CapabilityNotSupported):
                client.lookup("component", "MAX30011")
        assert counters["paths"] == ["/.well-known/agent-protocol.json"]

    def test_missing_blob_capability_stops_before_request(self, mock_endpoint):
        srv, counters = mock_endpoint(capabilities=("entity_lookup",))
        with AFPClient(srv.url) as client:
            with pytest.raises(CapabilityNotSupported):
                client.fetch_blob("a" * 64)
        assert counters["paths"] == ["/.well-known/agent-protocol.json"]

    def test_blob_without_http_template_refuses_to_guess(self, mock_endpoint):
        srv, counters = mock_endpoint(
            capabilities=("entity_lookup", "blob_access"),
            blob_methods=[{"method": "local_file", "template": "data/{sha256}.bin"}],
        )
        with AFPClient(srv.url) as client:
            with pytest.raises(CapabilityNotSupported):
                client.fetch_blob("a" * 64)
        assert counters["paths"] == ["/.well-known/agent-protocol.json"]

    def test_blob_download_follows_advertised_template(self, mock_endpoint):
        body = b"%PDF-1.4 mock datasheet"
        sha = hashlib.sha256(body).hexdigest()
        srv, counters = mock_endpoint(
            blob_methods=[{"method": "http", "template": "/files/{sha256}"}],
            blob_body=body,
        )
        with AFPClient(srv.url) as client:
            data = client.fetch_blob(sha)
        assert data == body
        assert f"/files/{sha}" in counters["paths"]  # followed the template
        assert not any(
            p.startswith("/api/v1/blob/") for p in counters["paths"]
        )  # no hardcoded blob path

    def test_blob_integrity_failure_raises(self, mock_endpoint):
        sha = hashlib.sha256(b"expected content").hexdigest()
        srv, _ = mock_endpoint(
            blob_methods=[{"method": "http", "template": "/files/{sha256}"}],
            blob_body=b"corrupted content",
        )
        with AFPClient(srv.url) as client:
            with pytest.raises(BlobIntegrityError) as excinfo:
                client.fetch_blob(sha)
        assert sha in str(excinfo.value)  # expected hash exposed in the error

    def test_remote_afp_error_carries_fields(self, mock_endpoint):
        srv, _ = mock_endpoint(
            lookup_response=(
                429,
                {
                    "error_code": "rate_limit",
                    "human_readable": "Too many requests",
                    "warnings": [],
                    "trace_id": "mock-trace-1",
                    "retry_after_sec": 30,
                },
            )
        )
        with AFPClient(srv.url) as client:
            with pytest.raises(RemoteAFPError) as excinfo:
                client.lookup("component", "MAX30011")
        assert not isinstance(excinfo.value, EntityMissing)  # not the 404 branch
        assert excinfo.value.error_code == "rate_limit"
        assert excinfo.value.retry_after_sec == 30
        assert excinfo.value.trace_id == "mock-trace-1"
        assert excinfo.value.human_readable == "Too many requests"

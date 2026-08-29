# -*- coding: utf-8 -*-
"""M5 Local-IPC binding tests (doc 08): every op, error shapes, NDJSON
framing, concurrency, and binding consistency across HTTP vs IPC
(verification matrix item 9: same key, identical entity content, only
meta.trace_id may differ)."""
from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "afp_client"))

from ipc_client import afp_ipc_call, pipe_connect  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="M5 implements the Windows named-pipe IPC transport"
)

# unique per run so parallel pytest sessions never fight over one pipe
PIPE_NAME = rf"\\.\pipe\afp-service-test-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def ipc_server():
    from ipc_binding import AFPIPCServer

    server = AFPIPCServer(pipe_name=PIPE_NAME)
    server.start()
    yield server
    server.stop()


def call(op, args=None, trace_id=None):
    return afp_ipc_call(PIPE_NAME, op, args, trace_id)


def raw_roundtrip(payload: bytes, expect_lines: int = 1) -> list[dict]:
    """Send raw bytes over one pipe connection; return parsed response lines.

    Used where afp_ipc_call cannot express the frame (malformed JSON, multi-
    line pipelining, protocol-level probes).
    """
    with pipe_connect(PIPE_NAME) as fh:
        view = memoryview(payload)
        while view:
            n = fh.write(view)
            view = view[n:]
        data = b""
        while data.count(b"\n") < expect_lines:
            chunk = fh.read(4096)
            if not chunk:
                break
            data += chunk
    return [json.loads(line) for line in data.splitlines() if line]


# ---------- discover (doc 08 section 3: op discover -> agent-protocol metadata) ----------

class TestDiscover:
    def test_discover_metadata(self, ipc_server):
        resp = call("discover", {}, trace_id="trace-ipc-disc")
        assert resp["ok"] is True
        meta = resp["metadata"]
        assert meta["protocol"] == "Agent-First-Protocol"
        assert meta["protocol_version"] == "v0.2"
        assert meta["service_name"]
        assert set(meta["capabilities"]) == {"entity_lookup", "entity_list", "blob_access"}
        assert "remote_fetch" not in meta["capabilities"]  # no over-claiming
        assert meta["domains"] == ["component"]
        assert meta["auth"]["mode"] == "none"
        assert meta["blob_access_methods"]
        assert meta["human_view"]["prefix"] == "/browser"
        # doc 08 section 1 port discovery: the pipe itself is advertised
        assert meta["extensions"]["ipc_pipe"] == PIPE_NAME


# ---------- lookup: found / miss / partial (matrix items 2-4 over IPC) ----------

class TestLookup:
    def test_lookup_found(self, ipc_server):
        resp = call("lookup", {"domain": "component", "key": "MAX30011"}, trace_id="trace-ipc-001")
        assert resp["ok"] is True
        e = resp["entity"]
        assert e["entity_id"] == "MAX30011"
        assert e["domain"] == "component"
        assert e["revision"] == "Rev-C"
        assert e["source_origin"] == "ADI official datasheet"
        assert e["sha256"] and len(e["sha256"]) == 64
        assert e["payload"]["part_number"] == "MAX30011"
        assert "pins" in e["payload"]
        assert "absolute_max_rating" in e["payload"]
        assert "forbidden_connect" in e["payload"]
        assert e["warnings"] == []
        assert e["meta"]["trace_id"] == "trace-ipc-001"  # doc 08 section 7.3 passthrough
        assert e["meta"]["timestamp"]

    def test_lookup_miss(self, ipc_server):
        resp = call("lookup", {"domain": "component", "key": "FAKE_CHIP_999"}, trace_id="trace-ipc-miss")
        assert resp["ok"] is False
        err = resp["error"]  # doc 08 section 2.3 shape, field-for-field
        assert err["error_code"] == "entity_missing"
        assert err["human_readable"]
        assert "FAKE_CHIP_999" in err["human_readable"]
        assert err["warnings"] == []
        assert err["trace_id"] == "trace-ipc-miss"  # trace flows into the error too
        assert err["retry_after_sec"] == 0

    def test_lookup_partial_match(self, ipc_server):
        resp = call("lookup", {"domain": "component", "key": "MAX3001"}, trace_id="trace-ipc-part")
        assert resp["ok"] is True
        e = resp["entity"]
        assert e["warnings"] != []  # doc 02 section 4: partial-match MUST warn
        assert "MAX30011" in e["payload"]["candidates"]
        assert e["source_origin"] is None  # P-2: no status-marker misuse
        assert e["sha256"] is None

    def test_lookup_revision_exact_and_miss(self, ipc_server):
        found = call("lookup", {"domain": "component", "key": "MAX30011", "revision": "Rev-C"})
        assert found["ok"] is True
        assert found["entity"]["revision"] == "Rev-C"
        miss = call("lookup", {"domain": "component", "key": "MAX30011", "revision": "Rev-Z"})
        assert miss["ok"] is False
        assert miss["error"]["error_code"] == "entity_missing"

    def test_trace_id_generated_when_absent(self, ipc_server):
        resp = call("lookup", {"domain": "component", "key": "ADS1299"})
        assert resp["ok"] is True
        assert resp["entity"]["meta"]["trace_id"].startswith("trace-")

    def test_unknown_domain(self, ipc_server):
        resp = call("lookup", {"domain": "doc", "key": "whatever"}, trace_id="t-dom")
        assert resp["ok"] is False
        assert resp["error"]["error_code"] == "entity_missing"
        assert "domain" in resp["error"]["human_readable"]


# ---------- list (doc 08 section 3: {items, total, offset, limit, has_more}) ----------

class TestList:
    def test_list_page(self, ipc_server):
        resp = call("list", {"domain": "component", "offset": 0, "limit": 3}, trace_id="trace-ipc-list")
        assert resp["ok"] is True
        assert resp["total"] == 5
        assert resp["offset"] == 0
        assert resp["limit"] == 3
        assert resp["has_more"] is True
        assert len(resp["items"]) == 3
        assert resp["items"][0]["entity_id"]
        assert resp["items"][0]["domain"] == "component"

    def test_list_defaults(self, ipc_server):
        resp = call("list", {"domain": "component"})
        assert resp["ok"] is True
        assert resp["offset"] == 0
        assert resp["limit"] == 20

    def test_list_invalid_params(self, ipc_server):
        for bad in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"offset": "x"}, {"limit": True}):
            resp = call("list", {"domain": "component", **bad})
            assert resp["ok"] is False, bad
            assert resp["error"]["error_code"] == "invalid_param", bad

    def test_list_unknown_domain(self, ipc_server):
        resp = call("list", {"domain": "nope"})
        assert resp["ok"] is False
        assert resp["error"]["error_code"] == "entity_missing"


# ---------- blob_resolve (doc 08 section 3: locator set) ----------

class TestBlobResolve:
    def _max_sha(self) -> str:
        return call("lookup", {"domain": "component", "key": "MAX30011"})["entity"]["sha256"]

    def test_blob_resolve_locator(self, ipc_server):
        sha = self._max_sha()
        resp = call("blob_resolve", {"sha256": sha}, trace_id="trace-ipc-blob")
        assert resp["ok"] is True
        loc = resp["locator"]
        assert loc["sha256"] == sha
        assert loc["mime_type"] == "application/pdf"
        assert loc["size_bytes"] > 0
        methods = {item["method"] for item in loc["locators"]}
        assert {"http", "local_file"} <= methods

    def test_blob_resolve_missing(self, ipc_server):
        resp = call("blob_resolve", {"sha256": "0" * 64}, trace_id="t-blob-miss")
        assert resp["ok"] is False
        assert resp["error"]["error_code"] == "entity_missing"
        assert resp["error"]["trace_id"] == "t-blob-miss"

    def test_blob_resolve_invalid_hash(self, ipc_server):
        resp = call("blob_resolve", {"sha256": "not-a-hash"})
        assert resp["ok"] is False
        assert resp["error"]["error_code"] == "invalid_param"


# ---------- fetch: no remote_fetch capability (task M5, doc 08 section 3) ----------

class TestFetch:
    def test_fetch_not_supported(self, ipc_server):
        resp = call("fetch", {"domain": "component", "key": "MAX30011"}, trace_id="trace-ipc-fetch")
        assert resp["ok"] is False
        err = resp["error"]
        assert err["error_code"] == "capability_not_supported"
        assert err["trace_id"] == "trace-ipc-fetch"
        assert err["human_readable"]


# ---------- protocol framing robustness (doc 08 section 7) ----------

class TestProtocolFrames:
    def test_invalid_json_line(self, ipc_server):
        resp = raw_roundtrip(b"this is not json\n")[0]
        assert resp["ok"] is False
        assert resp["error"]["error_code"] == "invalid_param"
        assert resp["error"]["trace_id"]  # even garbage gets a trace

    def test_unknown_op(self, ipc_server):
        resp = call("explode", {})
        assert resp["ok"] is False
        assert resp["error"]["error_code"] == "invalid_param"

    def test_args_must_be_object(self, ipc_server):
        frame = json.dumps({"op": "lookup", "trace_id": "t", "args": "nope"}).encode()
        resp = raw_roundtrip(frame + b"\n")[0]
        assert resp["ok"] is False
        assert resp["error"]["error_code"] == "invalid_param"

    def test_missing_key(self, ipc_server):
        resp = call("lookup", {"domain": "component"})
        assert resp["ok"] is False
        assert resp["error"]["error_code"] == "invalid_param"

    def test_multiline_requests_answered_in_order(self, ipc_server):
        # doc 08 section 7.1: multi-line requests are handled in order
        frame1 = json.dumps({"op": "lookup", "trace_id": "t-ml-1", "args": {"domain": "component", "key": "MAX30011"}})
        frame2 = json.dumps({"op": "lookup", "trace_id": "t-ml-2", "args": {"domain": "component", "key": "ADS1299"}})
        first, second = raw_roundtrip((frame1 + "\n" + frame2 + "\n").encode(), expect_lines=2)
        assert first["entity"]["entity_id"] == "MAX30011"
        assert first["entity"]["meta"]["trace_id"] == "t-ml-1"
        assert second["entity"]["entity_id"] == "ADS1299"
        assert second["entity"]["meta"]["trace_id"] == "t-ml-2"

    def test_concurrent_clients(self, ipc_server):
        # doc 08 section 7.2: requests are independent and may be concurrent;
        # the server keeps one worker thread per connection
        outcomes: list[tuple[str, bool]] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            for j in range(5):
                tid = f"trace-conc-{i}-{j}"
                try:
                    resp = call("lookup", {"domain": "component", "key": "MAX30011"}, trace_id=tid)
                    ok = (
                        resp["ok"]
                        and resp["entity"]["entity_id"] == "MAX30011"
                        and resp["entity"]["meta"]["trace_id"] == tid
                    )
                except Exception:
                    ok = False
                with lock:
                    outcomes.append((tid, ok))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert len(outcomes) == 20
        failed = [tid for tid, ok in outcomes if not ok]
        assert failed == []


# ---------- binding consistency: HTTP vs IPC (verification matrix item 9) ----------

class TestBindingConsistency:
    """Same key through HTTP and IPC: identical entity content, only
    meta.trace_id differs (timestamp is per-response generation time)."""

    def test_same_key_entity_identical(self, client, ipc_server):
        http = client.get(
            "/api/v1/component/lookup",
            params={"key": "MAX30011"},
            headers={"X-AFP-Trace-ID": "trace-http-9"},
        ).json()
        ipc = call(
            "lookup", {"domain": "component", "key": "MAX30011"}, trace_id="trace-ipc-9"
        )["entity"]
        assert http["meta"]["trace_id"] == "trace-http-9"
        assert ipc["meta"]["trace_id"] == "trace-ipc-9"
        for field in http:
            if field == "meta":
                continue
            assert http[field] == ipc[field], f"field {field!r} diverges across bindings"
        assert http["meta"]["trace_id"] != ipc["meta"]["trace_id"]

    def test_same_partial_key_identical(self, client, ipc_server):
        http = client.get("/api/v1/component/lookup", params={"key": "MAX3001"}).json()
        ipc = call("lookup", {"domain": "component", "key": "MAX3001"})["entity"]
        for field in ("entity_id", "domain", "revision", "source_origin", "sha256", "payload", "warnings"):
            assert http[field] == ipc[field], f"field {field!r} diverges across bindings"

    def test_list_identical(self, client, ipc_server):
        http = client.get("/api/v1/component/list", params={"limit": 3, "offset": 0}).json()
        ipc = call("list", {"domain": "component", "limit": 3, "offset": 0})
        for field in ("total", "offset", "limit", "has_more"):
            assert http[field] == ipc[field]
        assert len(ipc["items"]) == len(http["items"]) == 3
        for h_item, i_item in zip(http["items"], ipc["items"]):
            for field in h_item:
                if field == "meta":
                    continue
                assert h_item[field] == i_item[field]

    def test_blob_locator_identical(self, client, ipc_server):
        sha = call("lookup", {"domain": "component", "key": "MAX30011"})["entity"]["sha256"]
        http = client.get(f"/api/v1/blob/{sha}", headers={"Accept": "application/json"}).json()
        ipc = call("blob_resolve", {"sha256": sha})["locator"]
        assert http == ipc  # same locator set across bindings

    def test_same_miss_error_identical_code(self, client, ipc_server):
        http = client.get("/api/v1/component/lookup", params={"key": "FAKE_CHIP_999"})
        ipc = call("lookup", {"domain": "component", "key": "FAKE_CHIP_999"})
        assert http.status_code == 404
        assert http.json()["error_code"] == "entity_missing"
        assert ipc["ok"] is False
        assert ipc["error"]["error_code"] == "entity_missing"

# -*- coding: utf-8 -*-
"""M1 minimal-compliance tests: doc 10 verification matrix items 1-5
(discover / found / miss / partial / blob integrity) plus trace_id,
unknown-domain ADR-I2, and 422->400 normalization."""
from __future__ import annotations

import hashlib

import pytest
from pydantic import ValidationError

from afp_core import AFPError, Entity, ErrorCode, Meta


# ---------- L1 model unit tests (Task 1.1) ----------

class TestL1Models:
    def test_entity_requires_trace_id(self):
        with pytest.raises(ValidationError):
            Entity(entity_id="X", domain="component", payload={})

    def test_entity_rejects_blank_sha256(self):
        with pytest.raises(ValidationError):
            Entity(entity_id="X", domain="component", payload={}, sha256="", meta=Meta(trace_id="t"))

    def test_entity_accepts_null_sha256(self):
        e = Entity(entity_id="X", domain="component", payload={}, sha256=None, meta=Meta(trace_id="t"))
        assert e.sha256 is None

    def test_error_code_enum_is_closed(self):
        with pytest.raises(ValidationError):
            AFPError(error_code="bogus_code", trace_id="t")

    def test_error_code_enum_has_8_values(self):
        assert len(ErrorCode) == 8


# ---------- HTTP binding: discover (matrix item 1) ----------

class TestDiscover:
    def test_discover_schema(self, client):
        r = client.get("/.well-known/agent-protocol.json")
        assert r.status_code == 200
        meta = r.json()
        assert meta["protocol"] == "Agent-First-Protocol"
        assert meta["protocol_version"] == "v0.2"
        assert meta["service_name"]
        assert set(meta["capabilities"]) == {"entity_lookup", "entity_list", "blob_access"}
        assert "remote_fetch" not in meta["capabilities"]  # no over-claiming
        assert meta["domains"] == ["component"]
        assert meta["auth"]["mode"] == "none"
        assert meta["blob_access_methods"]
        assert meta["human_view"]["prefix"] == "/browser"
        assert "max-age=300" in r.headers["cache-control"]


# ---------- HTTP binding: lookup three states (matrix items 2-4) ----------

class TestLookup:
    def test_lookup_found(self, client):
        r = client.get("/api/v1/component/lookup", params={"key": "MAX30011"})
        assert r.status_code == 200
        e = r.json()
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
        assert e["meta"]["trace_id"]
        assert e["meta"]["timestamp"]
        assert "max-age=3600" in r.headers["cache-control"]

    def test_lookup_miss_is_404_not_200(self, client):
        r = client.get("/api/v1/component/lookup", params={"key": "FAKE_CHIP_999"})
        assert r.status_code == 404  # no business error wrapped in 200
        err = r.json()
        assert err["error_code"] == "entity_missing"
        assert err["trace_id"]
        assert "FAKE_CHIP_999" in err["human_readable"]

    def test_lookup_partial_match(self, client):
        r = client.get("/api/v1/component/lookup", params={"key": "MAX3001"})
        assert r.status_code == 200
        e = r.json()
        assert e["warnings"] != []  # doc 02 section 4: partial-match MUST carry warnings
        assert "MAX30011" in e["payload"]["candidates"]
        assert e["source_origin"] is None  # P-2: no status-marker misuse
        assert e["sha256"] is None

    def test_trace_id_passthrough(self, client):
        r = client.get(
            "/api/v1/component/lookup",
            params={"key": "MAX30011"},
            headers={"X-AFP-Trace-ID": "trace-demo-1"},
        )
        assert r.json()["meta"]["trace_id"] == "trace-demo-1"

    def test_trace_id_generated_when_absent(self, client):
        r = client.get("/api/v1/component/lookup", params={"key": "ADS1299"})
        tid = r.json()["meta"]["trace_id"]
        assert tid

    def test_unknown_domain_404(self, client):
        r = client.get("/api/v1/doc/lookup", params={"key": "whatever"})
        assert r.status_code == 404
        err = r.json()
        assert err["error_code"] == "entity_missing"
        assert "domain" in err["human_readable"]

    def test_missing_key_is_400_invalid_param(self, client):
        r = client.get("/api/v1/component/lookup")  # no key
        assert r.status_code == 400  # 422 normalized, no leak
        err = r.json()
        assert err["error_code"] == "invalid_param"
        assert err["trace_id"]

    def test_revision_mismatch_is_miss(self, client):
        r = client.get(
            "/api/v1/component/lookup",
            params={"key": "MAX30011", "revision": "Rev-Z"},
        )
        assert r.status_code == 404
        assert r.json()["error_code"] == "entity_missing"

    def test_revision_match_found(self, client):
        r = client.get(
            "/api/v1/component/lookup",
            params={"key": "MAX30011", "revision": "Rev-C"},
        )
        assert r.status_code == 200
        assert r.json()["revision"] == "Rev-C"


# ---------- HTTP binding: blob (matrix item 5) ----------

class TestBlob:
    def _max_sha(self, client) -> str:
        r = client.get("/api/v1/component/lookup", params={"key": "MAX30011"})
        return r.json()["sha256"]

    def test_blob_binary_integrity(self, client):
        sha = self._max_sha(client)
        r = client.get(f"/api/v1/blob/{sha}")
        assert r.status_code == 200
        assert r.headers["x-blob-sha256"] == sha
        assert "immutable" in r.headers["cache-control"]
        assert "max-age=31536000" in r.headers["cache-control"]
        assert r.content.startswith(b"%PDF-")
        assert hashlib.sha256(r.content).hexdigest() == sha  # recompute and compare

    def test_blob_locator_json(self, client):
        sha = self._max_sha(client)
        r = client.get(f"/api/v1/blob/{sha}", headers={"Accept": "application/json"})
        assert r.status_code == 200
        loc = r.json()
        assert loc["sha256"] == sha
        assert loc["mime_type"] == "application/pdf"
        assert loc["size_bytes"] > 0
        methods = {item["method"] for item in loc["locators"]}
        assert "http" in methods
        assert "local_file" in methods

    def test_blob_not_found_404(self, client):
        fake = "0" * 64
        r = client.get(f"/api/v1/blob/{fake}")
        assert r.status_code == 404
        assert r.json()["error_code"] == "entity_missing"

    def test_blob_invalid_hash_400(self, client):
        r = client.get("/api/v1/blob/not-a-hash")
        assert r.status_code == 400
        assert r.json()["error_code"] == "invalid_param"


# ---------- llms.txt / list / human_view isolation ----------

class TestMisc:
    def test_llms_txt(self, client):
        r = client.get("/llms.txt")
        assert r.status_code == 200
        assert "MAX30011" in r.text
        assert "Query Hints" in r.text

    def test_list_page_result(self, client):
        r = client.get("/api/v1/component/list", params={"limit": 3})
        assert r.status_code == 200
        page = r.json()
        assert page["total"] == 5
        assert page["limit"] == 3
        assert page["has_more"] is True
        assert len(page["items"]) == 3
        assert page["items"][0]["entity_id"]

    def test_human_view_isolated(self, client):
        r = client.get("/browser/component/MAX30011")
        assert r.status_code == 200
        assert "agents must use /api/v1/component/lookup" in r.text

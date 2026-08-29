# -*- coding: utf-8 -*-
"""M2 full-compliance tests: doc 10 verification matrix items 6 (rate limit)
and 7 (auth) plus list param validation, multi-revision coexistence, audit
log fields, and discover auth mirroring.

Every test builds its own app via create_app() against a throwaway
database / blob dir / audit log, so the shared M1 fixtures stay untouched.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from seed import ensure_seed
from server import Settings, create_app
from store import ComponentStore


class AppFactory:
    """Builds isolated app instances and remembers the last settings so
    tests can reach the underlying store directly."""

    def __init__(self, base_dir: Path):
        self.base_dir = base_dir
        self._seq = 0
        self.last_settings: Optional[Settings] = None

    def build(self, **overrides: Any) -> TestClient:
        self._seq += 1
        db_path = self.base_dir / f"afp_{self._seq}.db"
        blob_dir = self.base_dir / f"data_{self._seq}"
        audit_log = self.base_dir / f"audit_{self._seq}.jsonl"
        ensure_seed(str(db_path), str(blob_dir))
        settings = Settings(
            db_path=str(db_path),
            blob_dir=str(blob_dir),
            audit_log_path=str(audit_log),
            **overrides,
        )
        self.last_settings = settings
        return TestClient(create_app(settings))


@pytest.fixture()
def make_client(tmp_path: Path) -> AppFactory:
    return AppFactory(tmp_path)


MAX30011_REV_D_PAYLOAD = {
    "part_number": "MAX30011",
    "manufacturer": "ADI",
    "package": "WLP",
    "revision": "Rev-D",
    "description": "Single-lead biopotential AFE, silicon Rev-D errata rollup",
    "pins": [
        {"pin_num": "1", "pin_name": "VDD", "pin_type": "power_in"},
        {"pin_num": "2", "pin_name": "GND", "pin_type": "power"},
    ],
    "absolute_max_rating": [{"param": "VDD", "max": "3.6", "unit": "V"}],
    "forbidden_connect": ["NC pins must be left floating; external signals forbidden"],
}


def _insert_rev_d(settings: Settings) -> None:
    store = ComponentStore(settings.db_path)
    store.insert_component(
        part_number="MAX30011",
        manufacturer="ADI",
        category="EEG/ECG analog front-end",
        package="WLP",
        payload=MAX30011_REV_D_PAYLOAD,
        datasheet_sha256=None,
        source_file="ADI official datasheet Rev-D",
        fts_chunk="MAX30011 ADI Rev-D biopotential AFE errata",
    )


# ---------- list pagination & parameter validation (doc 06 section 2) ----------

class TestListValidation:
    def test_offset_negative_is_400_invalid_param(self, make_client):
        client = make_client.build()
        r = client.get("/api/v1/component/list", params={"offset": -1})
        assert r.status_code == 400
        err = r.json()
        assert err["error_code"] == "invalid_param"
        assert err["trace_id"]

    def test_limit_zero_is_400(self, make_client):
        client = make_client.build()
        r = client.get("/api/v1/component/list", params={"limit": 0})
        assert r.status_code == 400
        assert r.json()["error_code"] == "invalid_param"

    def test_limit_above_100_is_400(self, make_client):
        client = make_client.build()
        r = client.get("/api/v1/component/list", params={"limit": 101})
        assert r.status_code == 400
        assert r.json()["error_code"] == "invalid_param"

    def test_limit_non_numeric_is_400(self, make_client):
        client = make_client.build()
        r = client.get("/api/v1/component/list", params={"limit": "abc"})
        assert r.status_code == 400
        assert r.json()["error_code"] == "invalid_param"

    def test_defaults_are_offset0_limit20(self, make_client):
        client = make_client.build()
        r = client.get("/api/v1/component/list")
        assert r.status_code == 200
        page = r.json()
        assert page["offset"] == 0
        assert page["limit"] == 20
        assert page["total"] == 5
        assert len(page["items"]) == 5
        assert page["has_more"] is False

    def test_pagination_walk_no_overlap(self, make_client):
        client = make_client.build()
        pages = [
            client.get("/api/v1/component/list", params={"offset": o, "limit": 2}).json()
            for o in (0, 2, 4)
        ]
        assert [p["total"] for p in pages] == [5, 5, 5]
        assert [len(p["items"]) for p in pages] == [2, 2, 1]
        assert pages[0]["has_more"] is True
        assert pages[1]["has_more"] is True
        assert pages[2]["has_more"] is False
        seen: list[str] = []
        for p in pages:
            seen.extend(item["entity_id"] for item in p["items"])
        assert len(seen) == len(set(seen)) == 5

    def test_page_result_shape(self, make_client):
        client = make_client.build()
        page = client.get("/api/v1/component/list", params={"limit": 3}).json()
        assert {"items", "total", "offset", "limit", "has_more"} <= set(page.keys())
        first = page["items"][0]
        assert {"entity_id", "domain", "payload", "meta"} <= set(first.keys())
        assert first["meta"]["trace_id"]

    def test_unknown_domain_list_is_404(self, make_client):
        client = make_client.build()
        r = client.get("/api/v1/doc/list")
        assert r.status_code == 404
        assert r.json()["error_code"] == "entity_missing"


# ---------- rate limiting (doc 10 verification matrix item 6) ----------

class TestRateLimit:
    def test_burst_over_limit_gets_429_with_retry_after(self, make_client):
        client = make_client.build(
            rate_limit_enabled=True, rate_limit_max_req=3, rate_limit_window_sec=60
        )
        codes: list[int] = []
        last_body: dict = {}
        for _ in range(5):
            r = client.get("/api/v1/component/lookup", params={"key": "MAX30011"})
            codes.append(r.status_code)
            last_body = r.json()
        assert codes == [200, 200, 200, 429, 429]
        assert last_body["error_code"] == "rate_limit"
        assert last_body["retry_after_sec"] >= 1
        assert last_body["trace_id"]

    def test_rate_limit_disabled_by_default_knob_exists(self, make_client):
        # intranet default: limiter off (AFP_RATE_LIMIT_ENABLED=0), but the
        # Settings knob itself exists => doc 06 section 7 compliance
        client = make_client.build()
        assert client.app is not None
        settings = make_client.last_settings
        assert settings.rate_limit_enabled is False
        codes = [
            client.get("/api/v1/component/lookup", params={"key": "ADS1299"}).status_code
            for _ in range(70)  # above the default 60 req/min ceiling
        ]
        assert codes == [200] * 70


# ---------- api_key auth (doc 10 verification matrix item 7) ----------

class TestApiKeyAuth:
    KEY = "correct-horse-battery"

    def test_wrong_key_is_401_auth_failed(self, make_client):
        client = make_client.build(auth_mode="api_key", api_key=self.KEY)
        r = client.get(
            "/api/v1/component/lookup",
            params={"key": "MAX30011"},
            headers={"X-AFP-Api-Key": "wrong-key"},
        )
        assert r.status_code == 401
        err = r.json()
        assert err["error_code"] == "auth_failed"
        assert err["trace_id"]

    def test_missing_key_is_401(self, make_client):
        client = make_client.build(auth_mode="api_key", api_key=self.KEY)
        r = client.get("/api/v1/component/lookup", params={"key": "MAX30011"})
        assert r.status_code == 401
        assert r.json()["error_code"] == "auth_failed"

    def test_correct_key_passes(self, make_client):
        client = make_client.build(auth_mode="api_key", api_key=self.KEY)
        r = client.get(
            "/api/v1/component/lookup",
            params={"key": "MAX30011"},
            headers={"X-AFP-Api-Key": self.KEY},
        )
        assert r.status_code == 200
        assert r.json()["entity_id"] == "MAX30011"

    def test_discover_declares_api_key_mode(self, make_client):
        client = make_client.build(auth_mode="api_key", api_key=self.KEY)
        r = client.get("/.well-known/agent-protocol.json")  # discover stays public
        assert r.status_code == 200
        assert r.json()["auth"]["mode"] == "api_key"

    def test_discover_declares_none_when_auth_disabled(self, make_client):
        client = make_client.build()
        assert client.get("/.well-known/agent-protocol.json").json()["auth"]["mode"] == "none"

    def test_bootstrap_endpoints_stay_public_under_api_key(self, make_client):
        client = make_client.build(auth_mode="api_key", api_key=self.KEY)
        assert client.get("/.well-known/agent-protocol.json").status_code == 200
        assert client.get("/llms.txt").status_code == 200


# ---------- multi-revision coexistence (doc 10 section 2) ----------

class TestMultiRevision:
    def test_revisions_coexist_without_overwrite(self, make_client):
        client = make_client.build()
        _insert_rev_d(make_client.last_settings)
        r_old = client.get(
            "/api/v1/component/lookup", params={"key": "MAX30011", "revision": "Rev-C"}
        )
        r_new = client.get(
            "/api/v1/component/lookup", params={"key": "MAX30011", "revision": "Rev-D"}
        )
        assert r_old.status_code == 200
        assert r_new.status_code == 200
        assert r_old.json()["revision"] == "Rev-C"
        assert r_new.json()["revision"] == "Rev-D"
        assert r_old.json()["payload"]["description"] != r_new.json()["payload"]["description"]
        # both revisions live in the index: 5 seed rows + 1 extra revision
        page = client.get("/api/v1/component/list", params={"limit": 100}).json()
        assert page["total"] == 6
        max_rows = [i for i in page["items"] if i["entity_id"] == "MAX30011"]
        assert sorted(i["revision"] for i in max_rows) == ["Rev-C", "Rev-D"]

    def test_null_revision_returns_latest(self, make_client):
        client = make_client.build()
        _insert_rev_d(make_client.last_settings)
        r = client.get("/api/v1/component/lookup", params={"key": "MAX30011"})
        assert r.status_code == 200
        assert r.json()["revision"] == "Rev-D"  # latest insertion wins

    def test_unknown_revision_is_404_not_partial(self, make_client):
        client = make_client.build()
        _insert_rev_d(make_client.last_settings)
        r = client.get(
            "/api/v1/component/lookup", params={"key": "MAX30011", "revision": "Rev-Z"}
        )
        assert r.status_code == 404
        assert r.json()["error_code"] == "entity_missing"

    def test_duplicate_insert_keeps_original(self, make_client):
        client = make_client.build()
        _insert_rev_d(make_client.last_settings)
        _insert_rev_d(make_client.last_settings)  # same (part, revision): no-op
        store = ComponentStore(make_client.last_settings.db_path)
        rows, total = store.list_page(0, 100)
        max_rows = [r for r in rows if r["part_number"] == "MAX30011"]
        assert total == 6  # 5 seeds + Rev-D; the duplicate was ignored
        assert len(max_rows) == 2


# ---------- audit log (doc 06 section 7) ----------

class TestAuditLog:
    def _last_record(self, make_client: AppFactory) -> dict:
        path = Path(make_client.last_settings.audit_log_path)
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert lines
        return json.loads(lines[-1])

    def test_lookup_request_is_audited_with_full_fields(self, make_client):
        client = make_client.build()
        r = client.get(
            "/api/v1/component/lookup",
            params={"key": "MAX30011"},
            headers={"X-AFP-Trace-ID": "trace-audit-1"},
        )
        assert r.status_code == 200
        rec = self._last_record(make_client)
        assert rec["trace_id"] == "trace-audit-1"
        assert rec["caller"]
        assert rec["query_key"] == "MAX30011"
        assert rec["method"] == "GET"
        assert rec["path"] == "/api/v1/component/lookup"
        assert rec["status_code"] == 200
        assert rec["duration_ms"] >= 0
        assert rec["fetch_triggered"] is False  # reserved: always false pre-fetch_external
        assert rec["fetch_duration_ms"] == 0  # reserved: always 0 pre-fetch_external

    def test_failed_lookup_is_audited_too(self, make_client):
        client = make_client.build()
        r = client.get("/api/v1/component/lookup", params={"key": "GHOST_PART_404"})
        assert r.status_code == 404
        rec = self._last_record(make_client)
        assert rec["status_code"] == 404
        assert rec["query_key"] == "GHOST_PART_404"
        assert rec["trace_id"]

    def test_blob_request_audits_sha256_as_query_key(self, make_client):
        client = make_client.build()
        sha = client.get(
            "/api/v1/component/lookup", params={"key": "MAX30011"}
        ).json()["sha256"]
        r = client.get(f"/api/v1/blob/{sha}")
        assert r.status_code == 200
        rec = self._last_record(make_client)
        assert rec["query_key"] == sha
        assert rec["path"] == f"/api/v1/blob/{sha}"

# -*- coding: utf-8 -*-
"""Test bootstrap: sys.path for afp_service flat imports, env, idempotent seed."""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVICE = ROOT / "afp_service"
sys.path.insert(0, str(SERVICE))
CLIENT = ROOT / "afp_client"
sys.path.insert(0, str(CLIENT))
os.environ.setdefault("AFP_DB", str(ROOT / "afp.db"))
os.environ.setdefault("AFP_BLOB_DIR", str(ROOT / "data"))

from seed import ensure_seed  # noqa: E402

ensure_seed()

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session")
def client() -> TestClient:
    from server import app

    return TestClient(app)

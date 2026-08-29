# 11 · 参考实现（Reference Implementation）

> AFP‑v0.2 | 一个**最小合规、可运行**的 HTTP‑Web Binding 参考实现。
> 技术栈：Python 3.10+ / FastAPI / Uvicorn / SQLite（FTS5）
> 用途：作为实现 AFP 端点的起点模板；对接现有 SQLite 知识库（如 cezanne.db 的 component 表）。

---

## 1. 目录结构

```text
afp_service/
├── server.py          # FastAPI 入口
├── afp_core.py        # Entity/Error 模型 + schema
├── store.py           # SQLite 存取（component_meta / component_fts）
└── requirements.txt
```

---

## 2. requirements.txt

```text
fastapi>=0.110
uvicorn[standard]>=0.29
pydantic>=2.6
```

---

## 3. afp_core.py —— 核心模型

```python
"""AFP L1 核心模型：Entity / AFPError / 状态枚举"""
from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Optional
from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Meta(BaseModel):
    trace_id: str
    timestamp: str = Field(default_factory=utc_now)


class Entity(BaseModel):
    """AFP 实体对象（协议 L1）"""
    entity_id: str
    domain: str
    revision: Optional[str] = None
    source_origin: Optional[str] = None
    sha256: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    meta: Meta


class AFPError(BaseModel):
    """AFP 统一错误对象（协议 L1）"""
    error_code: str  # ok/entity_missing/partial_match/rate_limit/invalid_param/fetch_remote_failed/auth_failed/capability_not_supported
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
```

---

## 4. store.py —— SQLite 存取

```python
"""AFP 端点数据层：对接 SQLite component_meta / component_fts"""
from __future__ import annotations
import json
import sqlite3
from typing import Any, Optional


class ComponentStore:
    """基于 SQLite 的 component 域存储。

    表结构（可对接已有 cezanne.db）：
      component_meta(id, part_number, manufacturer, category, package,
                     json_payload TEXT, datasheet_sha256, source_file, create_at)
      component_fts(part_number, markdown_chunk, ..., tokenize='unicode61')
    """

    def __init__(self, db_path: str = "cezanne.db"):
        self.db_path = db_path
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS component_meta (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    part_number TEXT NOT NULL,
                    manufacturer TEXT,
                    category TEXT,
                    package TEXT,
                    json_payload TEXT NOT NULL,
                    datasheet_sha256 TEXT,
                    source_file TEXT,
                    create_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
            )
            c.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS component_fts USING fts5(
                    part_number, manufacturer, category, markdown_chunk,
                    tokenize='unicode61')"""
            )

    def lookup_exact(self, part: str) -> Optional[dict[str, Any]]:
        """精确命中 component_meta"""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM component_meta WHERE part_number = ? ORDER BY id DESC LIMIT 1",
                (part,),
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def lookup_fuzzy(self, q: str, limit: int = 5) -> list[dict[str, Any]]:
        """FTS 模糊命中 component_fts"""
        with self._conn() as c:
            try:
                rows = c.execute(
                    "SELECT part_number, manufacturer, category FROM component_fts "
                    "WHERE component_fts MATCH ? LIMIT ?",
                    (q, limit),
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                # FTS 查询语法问题兜底：退化为 LIKE
                like = f"%{q}%"
                rows = c.execute(
                    "SELECT part_number, manufacturer, category FROM component_fts "
                    "WHERE part_number LIKE ? OR manufacturer LIKE ? LIMIT ?",
                    (like, like, limit),
                ).fetchall()
                return [dict(r) for r in rows]
```

---

## 5. server.py —— FastAPI 端点（HTTP‑Web Binding）

```python
"""AFP HTTP‑Web Binding 参考实现（最小合规）"""
from __future__ import annotations
import hashlib
import json
import os
import uuid
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import Response

from afp_core import AFPError, Entity, Meta, PageResult
from store import ComponentStore

app = FastAPI(title="AFP Component Service", version="v0.2")
store = ComponentStore(db_path=os.environ.get("AFP_DB", "cezanne.db"))

# 二进制镜像目录（sha256 文件名）
BLOB_DIR = os.environ.get("AFP_BLOB_DIR", "./datasheet_mirror")

DISCOVERY = {
    "protocol": "Agent-First-Protocol",
    "protocol_version": "v0.2",
    "service_name": "Component-Datasheet-Service",
    "capabilities": ["entity_lookup", "entity_list", "blob_access", "remote_fetch"],
    "domains": ["component", "doc"],
    "auth": {"mode": "none"},
    "blob_access_methods": [
        {"method": "http", "template": "/api/v1/blob/{sha256}"},
        {"method": "local_file"},
    ],
    "human_view": {"available": True, "transport": "http", "prefix": "/browser"},
}


def new_trace(req: Request) -> str:
    return req.headers.get("x-afp-trace-id") or f"trace-{uuid.uuid4().hex[:12]}"


@app.get("/.well-known/agent-protocol.json")
def discover():
    return DISCOVERY


@app.get("/llms.txt")
def llms_txt():
    return Response(
        "# Component Datasheet Service\n\nUse entity_lookup with part number.\n",
        media_type="text/plain",
    )


@app.get("/api/v1/component/lookup")
def lookup(
    key: str = Query(...),
    revision: Optional[str] = Query(default=None),
    x_afp_trace_id: Optional[str] = Header(default=None),
    request: Request = None,
):
    trace = x_afp_trace_id or new_trace(request)

    row = store.lookup_exact(key)
    if row is None:
        # 二级：FTS 模糊
        cands = store.lookup_fuzzy(key)
        if cands:
            parts = [c["part_number"] for c in cands]
            return Entity(
                entity_id=key,
                domain="component",
                revision=None,
                source_origin="partial-match",
                payload={"candidates": parts},
                warnings=[
                    "partial-match: 非精确器件匹配，禁止直接用于 SKiDL 生成，"
                    "务必人工核对 datasheet"
                ],
                meta=Meta(trace_id=trace),
            )
        return AFPError(
            error_code="entity_missing",
            human_readable=f"No entity {key} in domain component",
            trace_id=trace,
        )

    payload = json.loads(row["json_payload"])
    return Entity(
        entity_id=row["part_number"],
        domain="component",
        revision=payload.get("revision"),
        source_origin=row.get("source_file") or "unknown",
        sha256=row.get("datasheet_sha256"),
        payload=payload,
        meta=Meta(trace_id=trace),
    )


@app.get("/api/v1/component/list")
def list_components(
    offset: int = 0,
    limit: int = Query(default=20, le=100),
    x_afp_trace_id: Optional[str] = Header(default=None),
    request: Request = None,
):
    trace = x_afp_trace_id or new_trace(request)
    with store._conn() as c:
        rows = c.execute(
            "SELECT part_number FROM component_meta LIMIT ? OFFSET ?", (limit, offset)
        ).fetchall()
        total = c.execute("SELECT COUNT(*) AS n FROM component_meta").fetchone()["n"]
    items = [
        Entity(
            entity_id=r["part_number"],
            domain="component",
            meta=Meta(trace_id=trace),
        )
        for r in rows
    ]
    return PageResult(
        items=items,
        total=total,
        offset=offset,
        limit=limit,
        has_more=(offset + limit) < total,
    )


@app.get("/api/v1/blob/{sha256}")
def blob(sha256: str, request: Request = None):
    """按 sha256 内容寻址返回二进制；响应头携带 X-Blob-Sha256"""
    if not _looks_like_sha256(sha256):
        raise HTTPException(status_code=400, detail="invalid sha256")
    path = os.path.join(BLOB_DIR, f"{sha256}.bin")
    if not os.path.exists(path):
        return AFPError(
            error_code="entity_missing",
            human_readable="blob not found",
            trace_id=new_trace(request),
        )
    with open(path, "rb") as f:
        data = f.read()
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"X-Blob-Sha256": sha256},
    )


def _looks_like_sha256(s: str) -> bool:
    return len(s) == 64 and all(ch in "0123456789abcdef" for ch in s.lower())


@app.get("/browser/component/{key}")
def human_view(key: str):
    """人类兼容视图（Agent 禁止从此通道取业务数据）"""
    return {
        "title": f"Component {key}",
        "note": "human view only; agents must use /api/v1/component/lookup",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=47100)
```

---

## 6. 运行方式

```bash
pip install -r requirements.txt
export AFP_DB=cezanne.db
export AFP_BLOB_DIR=./datasheet_mirror
python server.py
# 或 uvicorn server:app --host 127.0.0.1 --port 47100
```

---

## 7. 冒烟验证

```bash
# discover
curl -s http://127.0.0.1:47100/.well-known/agent-protocol.json

# lookup 精确
curl -s "http://127.0.0.1:47100/api/v1/component/lookup?key=MAX30011"

# lookup 缺失（应返回 entity_missing）
curl -s "http://127.0.0.1:47100/api/v1/component/lookup?key=FAKE_CHIP_999"

# list
curl -s "http://127.0.0.1:47100/api/v1/component/list?limit=5"

# blob
curl -s -o out.bin -H "X-AFP-Trace-ID: demo" \
  http://127.0.0.1:47100/api/v1/blob/a94a8fe5ccb19ba61c4c0873d391e987982fbbd3
```

---

## 8. 对齐合规清单

本参考实现满足 **最小合规**：discover / lookup（含 partial-match 警告）/ list / blob（sha256 + X-Blob-Sha256）/ 统一 AFPError / trace_id 透传 / human_view 隔离。

未实现（标记为后续项）：
- `remote_fetch` 回源（可后续用 FlareSolverr 网关叠加）
- 限流中间件（生产需加）
- MCP Binding（见 `07-MCP-Binding.md`）

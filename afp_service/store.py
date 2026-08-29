# -*- coding: utf-8 -*-
"""AFP store: SQLite persistence for the component domain.

Tables (doc 11 reference layout):
  component_meta(id, part_number, revision, manufacturer, category, package,
                 json_payload, datasheet_sha256, source_file, create_at)
  component_fts(part_number, manufacturer, category, markdown_chunk) FTS5 unicode61

M2 (doc 10 section 2): the same part_number may carry several revisions that
coexist without overwriting; lookup picks the exact revision or the newest.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator, Optional


class ComponentStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # transaction scope
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS component_meta (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    part_number TEXT NOT NULL,
                    revision TEXT,
                    manufacturer TEXT,
                    category TEXT,
                    package TEXT,
                    json_payload TEXT NOT NULL,
                    datasheet_sha256 TEXT,
                    source_file TEXT,
                    create_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"""
            )
            cols = {r["name"] for r in c.execute("PRAGMA table_info(component_meta)")}
            if "revision" not in cols:
                # M2 migration (doc 10 section 2: multi-revision coexistence):
                # revision becomes a real column, backfilled from the payload
                c.execute("ALTER TABLE component_meta ADD COLUMN revision TEXT")
                c.execute(
                    "UPDATE component_meta SET revision = json_extract(json_payload, '$.revision')"
                )
            # same (part_number, revision) pair must never overwrite (doc 10 section 2)
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_component_part_revision"
                " ON component_meta(part_number, revision)"
            )
            c.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS component_fts USING fts5(
                    part_number, manufacturer, category, markdown_chunk,
                    tokenize='unicode61')"""
            )

    def count(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) AS n FROM component_meta").fetchone()["n"]

    def insert_component(
        self,
        part_number: str,
        manufacturer: str,
        category: str,
        package: str,
        payload: dict[str, Any],
        datasheet_sha256: Optional[str],
        source_file: Optional[str],
        fts_chunk: str,
        revision: Optional[str] = None,
    ) -> None:
        """Insert one component revision.

        Inserting an already-stored (part_number, revision) pair is a no-op:
        revisions coexist and are never overwritten (doc 10 section 2).
        """
        rev = revision if revision is not None else payload.get("revision")
        with self._conn() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO component_meta (part_number, revision, manufacturer, category, package,"
                " json_payload, datasheet_sha256, source_file)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    part_number,
                    rev,
                    manufacturer,
                    category,
                    package,
                    json.dumps(payload, ensure_ascii=False),
                    datasheet_sha256,
                    source_file,
                ),
            )
            if cur.rowcount == 1:
                # index only freshly inserted rows: a duplicate revision must
                # not pollute the FTS index either
                c.execute(
                    "INSERT INTO component_fts (part_number, manufacturer, category, markdown_chunk)"
                    " VALUES (?, ?, ?, ?)",
                    (part_number, manufacturer, category, fts_chunk),
                )

    def lookup_exact(self, part: str, revision: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Exact lookup. revision=None picks the newest stored revision;
        a given revision picks that exact coexisting version."""
        with self._conn() as c:
            if revision is None:
                row = c.execute(
                    "SELECT * FROM component_meta WHERE part_number = ? ORDER BY id DESC LIMIT 1",
                    (part,),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT * FROM component_meta WHERE part_number = ? AND revision = ?"
                    " ORDER BY id DESC LIMIT 1",
                    (part, revision),
                ).fetchone()
            return dict(row) if row is not None else None

    def lookup_fuzzy(self, q: str, limit: int = 5) -> list[dict[str, Any]]:
        """Fuzzy candidates: substring match first, FTS prefix match as fallback."""
        like = f"%{q}%"
        with self._conn() as c:
            # DISTINCT: several revisions of one part must surface as a single candidate
            rows = c.execute(
                "SELECT DISTINCT part_number, manufacturer, category FROM component_meta"
                " WHERE part_number LIKE ? OR manufacturer LIKE ? OR category LIKE ?"
                " ORDER BY part_number LIMIT ?",
                (like, like, like, limit),
            ).fetchall()
            if rows:
                return [dict(r) for r in rows]
            try:
                rows = c.execute(
                    "SELECT part_number, manufacturer, category FROM component_fts"
                    " WHERE component_fts MATCH ? LIMIT ?",
                    (q + "*", limit),
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                return []

    def list_page(self, offset: int, limit: int) -> tuple[list[dict[str, Any]], int]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM component_meta ORDER BY part_number, revision LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            total = c.execute("SELECT COUNT(*) AS n FROM component_meta").fetchone()["n"]
        return [dict(r) for r in rows], total

    def list_index(self) -> list[dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT part_number, manufacturer, category, package FROM component_meta"
                " GROUP BY part_number ORDER BY part_number"
            ).fetchall()
        return [dict(r) for r in rows]

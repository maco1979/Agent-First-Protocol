# -*- coding: utf-8 -*-
r"""AFP Local-IPC Binding — Windows named pipe (protocol doc 08, milestone M5).

Transport choice (documented per task M5): doc 08 mandates NDJSON — every
frame must be a single line of legal JSON (section 7.1). The stdlib
``multiprocessing.connection.Listener`` also accepts ``\\.\pipe\\*``
addresses on Windows, but its wire format is pickle-based: it cannot carry
pure-JSON frames and would force a pickle-speaking client, contradicting
doc 08 section 6. pywin32 (win32pipe) is not in requirements.txt. => most
reliable zero-dependency option: raw kernel32 via ctypes.

  server : CreateNamedPipeW (byte-type, blocking) + ConnectNamedPipe; one
           thread per connection, and the accept loop keeps instantiating
           new pipe instances so several clients can stay attached at once
           (doc 08 section 7.2 — concurrent requests allowed).
  client : plain ``open(r'\\.\pipe\\...', 'r+b', buffering=0)`` (CreateFileW
           semantics), see afp_client/ipc_client.py.

Message format (doc 08 section 2, field-for-field):
  request  : {"op": ..., "trace_id": ..., "args": {...}}\n
  response : {"ok": true,  "entity" | "items" | "locator" | "metadata": ...}\n
  error    : {"ok": false, "error": {error_code, human_readable, warnings,
                                     trace_id, retry_after_sec}}\n   (2.3)

op enum (doc 08 section 3): discover / lookup / list / blob_resolve / fetch.
``fetch`` always answers capability_not_supported: this service implements
no remote_fetch (mirrors the HTTP discover capability list — no over-claim).

trace_id: passed through when the request carries one, generated otherwise
(doc 08 section 7.3).

The server shares the same SQLite store (same AFP_DB) as the HTTP binding;
binding consistency (verification matrix item 9) follows from that.
"""
from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable, Optional

from afp_core import AFPError, Entity, ErrorCode, Meta
from server import CAPABILITIES, PARTIAL_WARNING, SERVED_DOMAINS, Settings
from store import ComponentStore

DEFAULT_PIPE_NAME = r"\\.\pipe\afp-service"

# frame guard: a single NDJSON line longer than this is treated as abuse and
# the connection is dropped (framing cannot re-sync inside one line)
MAX_FRAME_BYTES = 4 * 1024 * 1024

_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    _kernel32.CreateNamedPipeW.argtypes = [
        wintypes.LPCWSTR,  # lpName
        wintypes.DWORD,  # dwOpenMode
        wintypes.DWORD,  # dwPipeMode
        wintypes.DWORD,  # nMaxInstances
        wintypes.DWORD,  # nOutBufferSize
        wintypes.DWORD,  # nInBufferSize
        wintypes.DWORD,  # nDefaultTimeOut
        wintypes.LPVOID,  # lpSecurityAttributes
    ]
    _kernel32.ConnectNamedPipe.restype = wintypes.BOOL
    _kernel32.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
    _kernel32.ReadFile.restype = wintypes.BOOL
    _kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,  # hFile
        wintypes.LPVOID,  # lpBuffer
        wintypes.DWORD,  # nNumberOfBytesToRead
        ctypes.POINTER(wintypes.DWORD),  # lpNumberOfBytesRead
        wintypes.LPVOID,  # lpOverlapped
    ]
    _kernel32.WriteFile.restype = wintypes.BOOL
    _kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,  # hFile
        wintypes.LPCVOID,  # lpBuffer
        wintypes.DWORD,  # nNumberOfBytesToWrite
        ctypes.POINTER(wintypes.DWORD),  # lpNumberOfBytesWritten
        wintypes.LPVOID,  # lpOverlapped
    ]
    _kernel32.DisconnectNamedPipe.restype = wintypes.BOOL
    _kernel32.DisconnectNamedPipe.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _PIPE_ACCESS_DUPLEX = 0x00000003
    _PIPE_TYPE_BYTE = 0x00000000
    _PIPE_READMODE_BYTE = 0x00000000
    _PIPE_WAIT = 0x00000000
    _PIPE_UNLIMITED_INSTANCES = 255
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    # client attached between CreateNamedPipeW and ConnectNamedPipe
    _ERROR_PIPE_CONNECTED = 535
    _READ_CHUNK = 64 * 1024


def _read_some(handle: int, size: int) -> Optional[bytes]:
    """One blocking ReadFile; None means the client went away (broken pipe)."""
    buf = ctypes.create_string_buffer(size)
    read = wintypes.DWORD(0)
    ok = _kernel32.ReadFile(handle, buf, size, ctypes.byref(read), None)
    if not ok:
        return None  # ERROR_BROKEN_PIPE / ERROR_EOF: end of stream
    return buf.raw[: read.value]


def _write_all(handle: int, data: bytes) -> bool:
    """One WriteFile on a blocking byte pipe (blocks until fully written)."""
    written = wintypes.DWORD(0)
    ok = _kernel32.WriteFile(handle, data, len(data), ctypes.byref(written), None)
    return bool(ok) and written.value == len(data)


def _is_sha256(s: str) -> bool:
    return len(s) == 64 and all(ch in "0123456789abcdef" for ch in s.lower())


class AFPIPCServer:
    """Named-pipe AFP binding sharing the HTTP binding's settings and store.

    Lifecycle: ``start()`` spawns the accept loop in a daemon thread and
    returns once the first pipe instance exists; ``stop()`` flips the stop
    flag and pokes one throwaway connection so an accept-blocked loop can
    wake up and exit.
    """

    def __init__(self, pipe_name: Optional[str] = None, settings: Optional[Settings] = None):
        if not _IS_WINDOWS:
            raise RuntimeError(
                "this M5 binding implements the Windows named-pipe transport; "
                "POSIX hosts should bind doc 08's Unix domain socket instead"
            )
        self.pipe_name = (
            pipe_name or os.environ.get("AFP_IPC_PATH") or DEFAULT_PIPE_NAME
        )
        self.settings = settings or Settings.from_env()
        # same SQLite database as the HTTP binding => binding consistency
        self.store = ComponentStore(self.settings.db_path)
        self.blob_dir = Path(self.settings.blob_dir)
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._main_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._handlers: dict[str, Callable[[dict, str], dict]] = {
            "discover": self._op_discover,
            "lookup": self._op_lookup,
            "list": self._op_list,
            "blob_resolve": self._op_blob_resolve,
            "fetch": self._op_fetch,
        }

    # ---------------- lifecycle ----------------

    def start(self, timeout: float = 5.0) -> None:
        """Serve in a background thread; returns once the pipe exists."""
        if self._main_thread is not None and self._main_thread.is_alive():
            return
        self._stop.clear()
        self._ready.clear()
        self._main_thread = threading.Thread(
            target=self.serve_forever, name="afp-ipc-accept", daemon=True
        )
        self._main_thread.start()
        if not self._ready.wait(timeout):
            self._stop.set()
            raise RuntimeError(
                f"IPC server could not create pipe {self.pipe_name!r} within {timeout}s"
            )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._main_thread is None:
            return
        deadline = time.monotonic() + timeout
        while self._main_thread.is_alive() and time.monotonic() < deadline:
            try:
                # poke one throwaway connection so an accept-blocked loop
                # wakes up, sees the stop flag and exits
                open(self.pipe_name, "r+b", buffering=0).close()
            except OSError:
                pass  # instance not there (yet/anymore) — retry until deadline
            self._main_thread.join(timeout=0.1)

    def serve_forever(self) -> None:
        """Accept loop: create instance -> wait client -> hand to a worker."""
        while not self._stop.is_set():
            handle = self._create_instance()
            if handle is None:
                time.sleep(0.05)  # transient failure / all instances busy
                continue
            self._ready.set()
            if not self._wait_client(handle):
                _kernel32.CloseHandle(handle)
                continue
            if self._stop.is_set():
                # the connection that woke us was the stop() poke: drop it
                _kernel32.CloseHandle(handle)
                return
            worker = threading.Thread(
                target=self._handle_conn, args=(handle,), daemon=True
            )
            worker.start()
            self._workers.append(worker)

    def _create_instance(self) -> Optional[int]:
        handle = _kernel32.CreateNamedPipeW(
            self.pipe_name,
            _PIPE_ACCESS_DUPLEX,
            _PIPE_TYPE_BYTE | _PIPE_READMODE_BYTE | _PIPE_WAIT,
            _PIPE_UNLIMITED_INSTANCES,
            _READ_CHUNK,
            _READ_CHUNK,
            0,  # default timeout
            None,
        )
        if not handle or handle == _INVALID_HANDLE_VALUE:
            return None
        return handle

    def _wait_client(self, handle: int) -> bool:
        if _kernel32.ConnectNamedPipe(handle, None):
            return True
        return ctypes.get_last_error() == _ERROR_PIPE_CONNECTED

    # ---------------- connection worker ----------------

    def _handle_conn(self, handle: int) -> None:
        """One worker thread per connection: NDJSON lines are processed and
        answered strictly in arrival order (doc 08 section 7.1).

        Note: no FlushFileBuffers here — on a byte-type pipe, WriteFile data
        is readable by the client as soon as the call returns, and Flush
        would block until the peer drains the buffer (an avoidable deadlock
        risk when the client stops reading at the trailing newline).
        """
        try:
            pending = bytearray()
            while not self._stop.is_set():
                nl = pending.find(b"\n")
                if nl < 0:
                    if len(pending) > MAX_FRAME_BYTES:
                        return  # frame guard tripped: drop the connection
                    chunk = _read_some(handle, _READ_CHUNK)
                    if not chunk:
                        return  # client closed the pipe
                    pending += chunk
                    continue
                line = bytes(pending[:nl])
                del pending[: nl + 1]
                response = self._process_line(line)
                payload = (json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8")
                if not _write_all(handle, payload):
                    return
        finally:
            _kernel32.DisconnectNamedPipe(handle)
            _kernel32.CloseHandle(handle)

    # ---------------- frame dispatch ----------------

    @staticmethod
    def _error(
        code: ErrorCode, human: str, trace: str, retry_after_sec: int = 0
    ) -> dict[str, Any]:
        # doc 08 section 2.3: {"ok": false, "error": AFPError}
        err = AFPError(
            error_code=code,
            human_readable=human,
            trace_id=trace,
            retry_after_sec=retry_after_sec,
        )
        return {"ok": False, "error": err.model_dump(mode="json")}

    def _process_line(self, raw: bytes) -> dict[str, Any]:
        """Parse one NDJSON request line into one response dict."""
        try:
            req = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return self._error(
                ErrorCode.INVALID_PARAM,
                "request frame is not valid UTF-8 JSON",
                f"trace-{uuid.uuid4().hex[:12]}",
            )
        if not isinstance(req, dict):
            return self._error(
                ErrorCode.INVALID_PARAM,
                "request frame must be a JSON object",
                f"trace-{uuid.uuid4().hex[:12]}",
            )
        trace = req.get("trace_id")
        if not isinstance(trace, str) or not trace:
            trace = f"trace-{uuid.uuid4().hex[:12]}"  # doc 08 section 7.3
        op = req.get("op")
        args = req.get("args")
        if args is None:
            args = {}
        if not isinstance(args, dict):
            return self._error(
                ErrorCode.INVALID_PARAM, "args must be a JSON object", trace
            )
        handler = self._handlers.get(op) if isinstance(op, str) else None
        if handler is None:
            return self._error(
                ErrorCode.INVALID_PARAM, f"unknown op: {op!r}", trace
            )
        try:
            return handler(args, trace)
        except Exception as exc:  # one bad frame must not kill the worker
            return self._error(
                ErrorCode.INVALID_PARAM, f"op {op} failed: {exc}", trace
            )

    # ---------------- op handlers (mirror server.py field-for-field) ----------------

    def _op_discover(self, args: dict, trace: str) -> dict[str, Any]:
        # mirrors HTTP GET /.well-known/agent-protocol.json field-for-field;
        # ipc_pipe in extensions = doc 08 section 1 "port discovery"
        metadata = {
            "protocol": "Agent-First-Protocol",
            "protocol_version": "v0.2",
            "service_name": "Component-Datasheet-Service",
            "capabilities": CAPABILITIES,
            "domains": SERVED_DOMAINS,
            "auth": {"mode": self.settings.auth_mode},
            "blob_access_methods": [
                {"method": "http", "template": "/api/v1/blob/{sha256}"},
                {"method": "local_file", "template": "data/{sha256}.bin"},
            ],
            "human_view": {"available": True, "transport": "http", "prefix": "/browser"},
            "extensions": {"service_owner": "zhuhai-lab", "ipc_pipe": self.pipe_name},
        }
        return {"ok": True, "metadata": metadata}

    def _op_lookup(self, args: dict, trace: str) -> dict[str, Any]:
        domain = args.get("domain")
        key = args.get("key")
        revision = args.get("revision")
        if not isinstance(domain, str) or not domain:
            return self._error(
                ErrorCode.INVALID_PARAM, "lookup requires a string domain", trace
            )
        if not isinstance(key, str) or not key:
            return self._error(
                ErrorCode.INVALID_PARAM, "lookup requires a non-empty string key", trace
            )
        if revision is not None and not isinstance(revision, str):
            return self._error(
                ErrorCode.INVALID_PARAM, "revision must be a string when given", trace
            )
        if domain not in SERVED_DOMAINS:
            # ADR-I2 (same as HTTP): unserved domain -> entity_missing
            return self._error(
                ErrorCode.ENTITY_MISSING,
                f"domain '{domain}' not served by this endpoint",
                trace,
            )
        row = self.store.lookup_exact(key, revision)
        if row is not None:
            payload = json.loads(row["json_payload"])
            entity = Entity(
                entity_id=row["part_number"],
                domain=domain,
                revision=row["revision"] or payload.get("revision"),
                source_origin=row.get("source_file") or "unknown",
                sha256=row.get("datasheet_sha256"),
                payload=payload,
                meta=Meta(trace_id=trace),
            )
            return {"ok": True, "entity": entity.model_dump(mode="json")}
        if revision is not None:
            # exact-revision miss is a plain miss: never degrade into
            # partial-match candidates (same rule as the HTTP binding)
            return self._error(
                ErrorCode.ENTITY_MISSING,
                f"No entity {key} revision {revision} in domain {domain}",
                trace,
            )
        candidates = self.store.lookup_fuzzy(key)
        if candidates:
            entity = Entity(
                entity_id=key,
                domain=domain,
                revision=None,
                source_origin=None,  # P-2: no status-marker misuse
                sha256=None,
                payload={"candidates": [c["part_number"] for c in candidates]},
                warnings=[PARTIAL_WARNING],
                meta=Meta(trace_id=trace),
            )
            return {"ok": True, "entity": entity.model_dump(mode="json")}
        return self._error(
            ErrorCode.ENTITY_MISSING,
            f"No entity {key} in domain {domain}",
            trace,
        )

    def _op_list(self, args: dict, trace: str) -> dict[str, Any]:
        domain = args.get("domain")
        if not isinstance(domain, str) or not domain:
            return self._error(
                ErrorCode.INVALID_PARAM, "list requires a string domain", trace
            )
        if domain not in SERVED_DOMAINS:
            return self._error(
                ErrorCode.ENTITY_MISSING,
                f"domain '{domain}' not served by this endpoint",
                trace,
            )
        offset = args.get("offset", 0)
        limit = args.get("limit", 20)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            return self._error(
                ErrorCode.INVALID_PARAM, "offset must be an integer >= 0", trace
            )
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            return self._error(
                ErrorCode.INVALID_PARAM, "limit must be an integer in [1, 100]", trace
            )
        rows, total = self.store.list_page(offset, limit)
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
        return {
            "ok": True,
            "items": [e.model_dump(mode="json") for e in items],
            "total": total,
            "offset": offset,
            "limit": limit,
            "has_more": (offset + limit) < total,
        }

    def _op_blob_resolve(self, args: dict, trace: str) -> dict[str, Any]:
        # same locator set as HTTP GET /api/v1/blob/{sha256} with
        # Accept: application/json (ADR-I1, doc 04 section 2.4)
        sha = args.get("sha256")
        if not isinstance(sha, str) or not _is_sha256(sha):
            return self._error(
                ErrorCode.INVALID_PARAM,
                f"invalid sha256 argument: {sha!r}",
                trace,
            )
        path = self.blob_dir / f"{sha}.bin"
        if not path.is_file():
            return self._error(
                ErrorCode.ENTITY_MISSING, f"blob {sha} not found", trace
            )
        data = path.read_bytes()
        mime = "application/pdf" if data.startswith(b"%PDF-") else "application/octet-stream"
        locator = {
            "sha256": sha,
            "mime_type": mime,
            "size_bytes": len(data),
            "locators": [
                {"method": "http", "url": f"/api/v1/blob/{sha}"},
                {"method": "local_file", "path": f"data/{sha}.bin"},
            ],
        }
        return {"ok": True, "locator": locator}

    def _op_fetch(self, args: dict, trace: str) -> dict[str, Any]:
        # doc 08 section 3 lists fetch, but this service implements no
        # remote_fetch: answer capability_not_supported instead of
        # over-claiming (mirrors the HTTP discover capability list)
        return self._error(
            ErrorCode.CAPABILITY_NOT_SUPPORTED,
            "remote_fetch is not implemented by this service; discover declares "
            "no remote_fetch capability — use blob_access locators instead",
            trace,
        )


if __name__ == "__main__":
    _server = AFPIPCServer()
    print(f"AFP IPC binding listening on {_server.pipe_name}")
    try:
        _server.serve_forever()
    except KeyboardInterrupt:
        _server.stop()
        print("AFP IPC binding stopped")

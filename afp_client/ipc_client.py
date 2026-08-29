# -*- coding: utf-8 -*-
r"""AFP Local-IPC client (doc 08 section 6): afp_ipc_call() over a Windows
named pipe (``\\.\pipe\...``) or, on POSIX hosts, a Unix domain socket —
doc 08 section 1 allows both carriers for this binding.

One call = one connection: send a single NDJSON request line, read a single
NDJSON response line, close. The server answers every request independently
(doc 08 section 7.2), so no session state is kept here.

Named-pipe connections use raw CreateFileW instead of open(): the CRT layer
maps ERROR_PIPE_BUSY (all server instances taken — a normal transient during
the server's accept loop) to a bare EINVAL without a winerror attribute,
making it indistinguishable from a genuinely invalid path. With CreateFileW
we get the real error code and can apply the canonical pipe-client pattern:
WaitNamedPipeW + retry until a free instance appears.
"""
from __future__ import annotations

import ctypes
import json
import os
import socket
import time
import uuid
from typing import Any, BinaryIO, Optional

_WINDOWS_PIPE_PREFIX = "\\\\.\\pipe\\"

_IS_WINDOWS = os.name == "nt"

if _IS_WINDOWS:
    import msvcrt
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,  # lpFileName
        wintypes.DWORD,  # dwDesiredAccess
        wintypes.DWORD,  # dwShareMode
        wintypes.LPVOID,  # lpSecurityAttributes
        wintypes.DWORD,  # dwCreationDisposition
        wintypes.DWORD,  # dwFlagsAndAttributes
        wintypes.HANDLE,  # hTemplateFile
    ]
    _kernel32.WaitNamedPipeW.restype = wintypes.BOOL
    _kernel32.WaitNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _OPEN_EXISTING = 3
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _ERROR_PIPE_BUSY = 231
    _ERROR_FILE_NOT_FOUND = 2


def new_trace_id() -> str:
    return f"trace-{uuid.uuid4().hex[:12]}"


def afp_ipc_call(
    pipe_path: str,
    op: str,
    args: Optional[dict] = None,
    trace_id: Optional[str] = None,
) -> dict[str, Any]:
    r"""Call one AFP IPC op and return the parsed response line.

    ``pipe_path`` is a named pipe (``\\.\pipe\afp-service``) on Windows or a
    Unix socket path elsewhere. ``trace_id`` is passed through when given and
    generated otherwise (doc 08 section 7.3).
    """
    request = {
        "op": op,
        "trace_id": trace_id if isinstance(trace_id, str) and trace_id else new_trace_id(),
        "args": args or {},
    }
    payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")
    data = _roundtrip(pipe_path, payload)
    if not data.strip():
        raise ConnectionError(
            f"IPC peer {pipe_path} closed the connection without a response"
        )
    return json.loads(data.decode("utf-8"))


def pipe_connect(pipe_path: str, timeout_ms: int = 5000) -> BinaryIO:
    """Open one connection to the named-pipe server (Windows only).

    Returns an unbuffered binary file object (closing it closes the pipe).
    Exposed for protocol-level tests that must send raw frames.
    """
    if not _IS_WINDOWS:
        raise RuntimeError("pipe_connect() implements the Windows named-pipe transport")
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        handle = _kernel32.CreateFileW(
            pipe_path,
            _GENERIC_READ | _GENERIC_WRITE,
            0,
            None,
            _OPEN_EXISTING,
            0,
            None,
        )
        if handle and handle != _INVALID_HANDLE_VALUE:
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
            if fd == -1:
                _kernel32.CloseHandle(handle)
                raise OSError(f"open_osfhandle failed for pipe {pipe_path}")
            return os.fdopen(fd, "r+b", buffering=0)
        err = ctypes.get_last_error()
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"IPC pipe {pipe_path} not reachable within {timeout_ms}ms "
                f"(last CreateFileW error {err})"
            )
        if err == _ERROR_PIPE_BUSY:
            # every instance is attached: wait for the accept loop to free /
            # create one, then retry (canonical named-pipe client pattern)
            _kernel32.WaitNamedPipeW(pipe_path, 200)
        else:
            # ERROR_FILE_NOT_FOUND: no listening instance yet (startup or
            # accept-loop race) — brief sleep and retry
            time.sleep(0.01)


def _roundtrip(path: str, payload: bytes) -> bytes:
    if _IS_WINDOWS and path.startswith(_WINDOWS_PIPE_PREFIX):
        return _named_pipe_roundtrip(path, payload)
    return _unix_socket_roundtrip(path, payload)


def _named_pipe_roundtrip(path: str, payload: bytes, connect_timeout_ms: int = 5000) -> bytes:
    with pipe_connect(path, connect_timeout_ms) as fh:
        view = memoryview(payload)
        while view:
            n = fh.write(view)
            view = view[n:]
        data = b""
        while not data.endswith(b"\n"):
            chunk = fh.read(4096)
            if not chunk:
                break  # peer closed mid-frame; caller validates the line
            data += chunk
    return data


def _unix_socket_roundtrip(path: str, payload: bytes) -> bytes:
    # doc 08 section 6 reference client, verbatim transport behavior
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(path)
        s.sendall(payload)
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return data

# 08 · Local‑IPC Binding 规范

> AFP‑v0.2 | 本机高性能调用绑定：Unix‑socket / 命名管道。无 HTTP 开销，适合守护进程、高频查询。

---

## 1. 设计要点

- 载体：Unix Domain Socket（推荐）或命名管道。
- 协议：单条请求 = 一行 JSON（换行分隔的 NDJSON 风格），响应 = 一行 JSON。
- 端口发现：socket 路径由约定或环境变量 / 发现文件提供。

---

## 2. 报文格式

### 2.1 请求

```json
{ "op": "lookup", "trace_id": "trace-ipc-001", "args": { "domain": "component", "key": "MAX30011" } }
```

### 2.2 响应

```json
{ "ok": true, "entity": { "entity_id": "MAX30011", "domain": "component", "payload": {}, "meta": { "trace_id": "trace-ipc-001", "timestamp": "2026-08-29T08:00:00Z" } } }
```

### 2.3 错误响应

```json
{ "ok": false, "error": { "error_code": "entity_missing", "human_readable": "...", "warnings": [], "trace_id": "trace-ipc-001", "retry_after_sec": 0 } }
```

---

## 3. op 枚举

| op | args | 返回 |
|---|---|---|
| discover | {} | agent-protocol 元数据 |
| lookup | {domain, key, revision?} | entity / error |
| list | {domain, filter?, offset, limit} | {items, total, offset, limit, has_more} |
| blob_resolve | {sha256} | locator 集合 |
| fetch | {domain, key} | entity / error |

---

## 4. 错误码

与 L1 统一 error_code 枚举完全一致。

---

## 5. 鉴权

- 本机 IPC 默认 `auth.mode=none`，以文件权限控制访问。
- 若启用鉴权：请求 JSON 携带 `auth` 字段。

---

## 6. 示例（Python client）

```python
import socket, json

def afp_ipc_call(sock_path, op, args, trace_id):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(sock_path)
        req = json.dumps({"op": op, "trace_id": trace_id, "args": args}) + "\n"
        s.sendall(req.encode())
        data = b""
        while not data.endswith(b"\n"):
            data += s.recv(4096)
        return json.loads(data)

resp = afp_ipc_call("/tmp/afp-service.sock", "lookup",
                    {"domain": "component", "key": "MAX30011"},
                    "trace-ipc-001")
print(resp)
```

---

## 7. 约束

1. 报文必须是合法 JSON，单行；多行按顺序处理。
2. 每个请求独立响应，不依赖顺序（可并发）。
3. trace_id 必须透传。

# 09 · Embedded‑Serial Binding 规范

> AFP‑v0.2 | 面向嵌入式硬件端点的串口绑定。用于单片机、传感器节点等资源受限设备暴露 AFP 能力。

---

## 1. 设计要点

- 载体：UART 串口（波特率通常 115200，可配置）。
- 协议：帧结构封装 JSON 报文。
- 适用：嵌入式设备作为 AFP 端点，向主机 Agent 提供实体查询。

---

## 2. 帧结构

```
[0xAF] [0x01] [len:2B BE] [payload JSON] [crc8]
```

| 字段 | 长度 | 说明 |
|---|---|---|
| 帧头 | 2B | 固定 `0xAF 0x01` |
| 长度 | 2B | payload 字节数（大端） |
| payload | N | JSON 报文 |
| CRC8 | 1B | payload 校验 |

---

## 3. 报文（与 Local‑IPC 一致的 op 语义）

请求：
```json
{ "op": "lookup", "trace_id": "ser-001", "args": { "domain": "sensor", "key": "node-07" } }
```

响应：
```json
{ "ok": true, "entity": { "entity_id": "node-07", "domain": "sensor", "payload": { "temp_c": 24.5 }, "meta": { "trace_id": "ser-001", "timestamp": "..." } } }
```

---

## 4. 支持的原语

| op | 说明 |
|---|---|
| discover | 返回精简版 agent-protocol（嵌入式可裁字段） |
| lookup | 实体查询（嵌入式核心能力） |

> 资源受限设备可不实现 list / blob / fetch，未支持时返回 `capability_not_supported`。

---

## 5. 精简 discover 示例

```json
{
  "protocol": "Agent-First-Protocol",
  "protocol_version": "v0.2",
  "service_name": "Sensor-Node-07",
  "capabilities": ["entity_lookup"],
  "domains": ["sensor"],
  "auth": { "mode": "none" }
}
```

---

## 6. 约束

1. 错误码与 L1 统一枚举一致。
2. trace_id 透传；设备资源有限时至少返回 error_code + trace_id。
3. 校验失败（CRC 错误）静默丢弃该帧，不发错误响应。
4. 超时重传由上层处理，本绑定不定义重传逻辑。

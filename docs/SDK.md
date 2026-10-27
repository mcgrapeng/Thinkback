# SDK Examples

Client examples for the Thinkback memory service. For the HTTP contract itself see
[the API reference](API.md); the gRPC contract lives in `proto/memory.proto`.

The business surface needs no API key, no scope, and no tenant header — Thinkback runs on an
internal network and the network is the trust boundary. Two headers are still worth sending:

| Header | Value |
| --- | --- |
| `Idempotency-Key` | `idem_001` — replay-safe writes (24h cache) |
| `X-Trace-Id` | `trace_001` — correlates your logs with the service logs |

Request bodies are the service's own Pydantic schemas (`thinkback.memory.schemas`). Unknown
fields are rejected, so removed fields such as `intent` must not be sent.

Local development runs the API on `http://127.0.0.1:7002` (`make dev`); in Docker and
Kubernetes it listens on `:8000`.

---

## Python

```python
import httpx

client = httpx.Client(base_url="http://127.0.0.1:7002", timeout=30.0)

# Append a memory round (triggers the L1/L2/L3 pipeline)
response = client.post(
    "/memory/append",
    headers={"Idempotency-Key": "idem-001"},
    json={
        "request_id": "req-001",
        "user_id": "alice",
        "session_id": "support-001",
        "round_id": "round-001",
        "source_timestamp": "2026-01-15T10:00:00Z",
        "messages": [
            {
                "message_id": "m1",
                "role": "user",
                "content": "I prefer dark mode and vim keybindings.",
                "timestamp": "2026-01-15T10:00:00Z",
            },
            {
                "message_id": "m2",
                "role": "assistant",
                "content": "Noted. I will remember that.",
                "timestamp": "2026-01-15T10:00:02Z",
            },
        ],
    },
)
print(response.json()["task_id"])  # track async L3 extraction

# Recall relevant memories
result = client.post(
    "/memory/recall",
    json={
        "user_id": "alice",
        "session_id": "support-002",
        "query": "What UI preferences does this user have?",
        "l3_limit": 5,
    },
).json()
if result["degraded"]:
    print("degraded:", result["degradation_reasons"])
for item in result["items"]:
    print(f"[{item['layer']}] {item['content']}")
```

## TypeScript

```typescript
const base = process.env.THINKBACK_API_URL ?? "http://127.0.0.1:7002";

// Append a memory round
const appended = await fetch(`${base}/memory/append`, {
  method: "POST",
  headers: { "Content-Type": "application/json", "Idempotency-Key": "idem-001" },
  body: JSON.stringify({
    request_id: "req-001",
    user_id: "alice",
    session_id: "support-001",
    round_id: "round-001",
    source_timestamp: "2026-01-15T10:00:00Z",
    messages: [
      { message_id: "m1", role: "user",
        content: "I prefer dark mode and vim keybindings.",
        timestamp: "2026-01-15T10:00:00Z" },
      { message_id: "m2", role: "assistant",
        content: "Noted. I will remember that.",
        timestamp: "2026-01-15T10:00:02Z" },
    ],
  }),
});

// Recall with degradation detection
const result = await fetch(`${base}/memory/recall`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    user_id: "alice",
    session_id: "support-002",
    query: "What UI preferences does this user have?",
    l3_limit: 5,
  }),
}).then((res) => res.json());

if (result.degraded) {
  console.warn("Memory service degraded:", result.degradation_reasons);
}
for (const item of result.items) {
  console.log(`[${item.layer}] ${item.content}`);
}
```

## cURL

```bash
curl -X POST http://127.0.0.1:7002/memory/recall \
  -H "Content-Type: application/json" \
  -H "X-Trace-Id: trace-001" \
  -d '{
    "user_id": "u_8421",
    "session_id": "s_001",
    "query": "用户偏好什么编辑器?",
    "l3_limit": 5
  }'
```

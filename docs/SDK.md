# SDK Examples

Client examples for the Thinkback v1 public API. For the protocol itself see
[the v1 integration spec](specs/2026-10-08-thinkback-integration-protocol-v1.md).

All v1 requests need:

| Header | Value |
| --- | --- |
| `Authorization` | `Bearer tbk_live_xxxxxxxx` |
| `X-Tenant-Id` | `tenant_001` |
| `Idempotency-Key` | `idem_001` (replay-safe) |

Scopes required: `memory:append`, `memory:recall`, `memory:read`, `memory:update`,
`memory:delete`. Generate a key in the admin console under `/integration`.

---

## Python

```python
from thinkback import Thinkback

client = Thinkback(api_key="tbk_live_xxxxxxxx", tenant_id="tenant_001")

# Append a memory round (triggers L1/L2/L3 pipeline)
response = client.memory.append(
    request_id="req-001",
    user_id="alice",
    session_id="support-001",
    round_id="round-001",
    messages=[
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
)
print(response.task_id)  # track async L3 extraction

# Recall relevant memories
result = client.memory.recall(
    user_id="alice",
    session_id="support-002",
    query="What UI preferences does this user have?",
    intent="chat",
    l3_limit=5,
)
for item in result.items:
    print(f"[{item.layer}] {item.content}")
```

## TypeScript

```typescript
import { Thinkback } from "@thinkback/sdk";

const client = new Thinkback({
  apiKey: process.env.THINKBACK_API_KEY,
  tenantId: "tenant_001",
});

// Append a memory round
await client.memory.append({
  requestId: "req-001",
  userId: "alice",
  sessionId: "support-001",
  roundId: "round-001",
  messages: [
    { messageId: "m1", role: "user",
      content: "I prefer dark mode and vim keybindings.",
      timestamp: "2026-01-15T10:00:00Z" },
    { messageId: "m2", role: "assistant",
      content: "Noted. I will remember that.",
      timestamp: "2026-01-15T10:00:02Z" },
  ],
});

// Recall with degradation detection
const result = await client.memory.recall({
  userId: "alice",
  sessionId: "support-002",
  query: "What UI preferences does this user have?",
  intent: "chat",
  l3Limit: 5,
});

if (result.degraded) {
  console.warn("Memory service degraded:", result.degradationReasons);
}
for (const item of result.items) {
  console.log(`[${item.layer}] ${item.content}`);
}
```

## cURL

```bash
# Generate an API key from the admin console at /integration
curl -X POST https://your-thinkback/v1/memory/recall \
  -H "Authorization: Bearer tbk_live_xxxxxxxx" \
  -H "X-Tenant-Id: tenant_001" \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: idem_001" \
  -d '{
    "user_id": "u_8421",
    "session_id": "s_001",
    "query": "用户偏好什么编辑器?",
    "intent": "chat",
    "l3_limit": 5
  }'
```
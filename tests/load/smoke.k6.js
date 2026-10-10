// k6 冒烟压测：真实后端 + API Key（不进 make check）。
//
//   make load-smoke BASE_URL=http://127.0.0.1:7002 API_KEY=tbk_test_xxx
//
// 口径：低并发 30 秒验证 /v1 读写延迟与错误率；全量压力/故障注入见
// tests/script/real_mem0_p0_*.py。

import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:7002";
const API_KEY = __ENV.API_KEY || "";

export const options = {
  vus: 4,
  duration: "30s",
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{op:append}": ["p(95)<1000"],
    "http_req_duration{op:recall}": ["p(95)<500"],
  },
};

function headers(iter) {
  return {
    "Content-Type": "application/json",
    Authorization: `Bearer ${API_KEY}`,
    "Idempotency-Key": `k6-${__VU}-${iter}`,
  };
}

export default function () {
  const ts = new Date().toISOString();
  const append = http.post(
    `${BASE_URL}/v1/memory/append`,
    JSON.stringify({
      request_id: `k6-${__VU}-${__ITER}`,
      user_id: "k6-user",
      session_id: "k6-session",
      round_id: `k6-round-${__VU}-${__ITER}`,
      messages: [
        { message_id: "m1", role: "user", content: "压测消息：我喜欢被叫阿鹏", timestamp: ts },
      ],
      source_timestamp: ts,
    }),
    { headers: headers(__ITER), tags: { op: "append" } },
  );
  check(append, { "append 2xx": (r) => r.status >= 200 && r.status < 300 });

  const recall = http.post(
    `${BASE_URL}/v1/memory/recall`,
    JSON.stringify({
      user_id: "k6-user",
      session_id: "k6-session",
      query: "叫我什么",
      intent: "chat",
    }),
    { headers: headers(__ITER), tags: { op: "recall" } },
  );
  check(recall, { "recall 2xx": (r) => r.status >= 200 && r.status < 300 });
  sleep(0.2);
}

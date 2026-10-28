// k6 冒烟压测：真实后端（内网免鉴权，/memory/* 为唯一业务面，不进 make check）。
//
//   make load-smoke BASE_URL=http://127.0.0.1:7002
//
// 口径：低并发 30 秒验证读写延迟、错误率**与记忆正确性**——
//   - append 的后台任务轮询到终态且必须是 completed（写入真的落盘了）；
//   - recall 必须未降级（degraded=false，L3 后端可达，没有退回 L1/L2 兜底）；
//   - recall 必须召回到本轮写入的事实（内容含本轮唯一标记）。
// 只看 2xx 与延迟的压测测不出"写进去了但召不回来"，那正是线上最难查的一类故障。
// 全量压力 / 故障注入见 tests/script/real_mem0_p0_*.py。

import http from "k6/http";
import { check, sleep } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://127.0.0.1:7002";
// 后台任务最长轮询次数与间隔：30 次 × 0.2s = 6s 预算，够 L3 落盘走完。
const TASK_POLL_MAX = 30;
const TASK_POLL_INTERVAL = 0.2;
const TERMINAL_TASK_STATUSES = ["completed", "failed", "dead_letter"];

export const options = {
  vus: 4,
  duration: "30s",
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{op:append}": ["p(95)<1000"],
    "http_req_duration{op:recall}": ["p(95)<500"],
    // 正确性也要决定退出码：写进去了却召不回来（check 失败）在只判延迟
    // 时会「绿着」通过 —— 那正是线上最难查的一类故障。
    checks: ["rate==1.0"],
  },
};

function headers(iter) {
  return {
    "Content-Type": "application/json",
    "Idempotency-Key": `k6-${__VU}-${iter}`,
  };
}

// 轮询后台任务到终态；返回任务对象，未进入终态返回 null。
function pollTask(taskId, iter) {
  for (let i = 0; i < TASK_POLL_MAX; i++) {
    const res = http.get(`${BASE_URL}/memory/tasks/${encodeURIComponent(taskId)}`, {
      headers: headers(iter),
      tags: { op: "task" },
    });
    if (res.status !== 200) {
      return null;
    }
    const status = jsonField(res, "status");
    if (status !== undefined && TERMINAL_TASK_STATUSES.indexOf(status) !== -1) {
      return { status: status };
    }
    sleep(TASK_POLL_INTERVAL);
  }
  return null;
}

// 召回正文（L1/L2/L3 各层的 content 拼起来）。
function recalledText(response) {
  const items = jsonField(response, "items");
  if (!Array.isArray(items)) {
    return "";
  }
  return items.map((item) => item.content || "").join("\n");
}

// 容错取 JSON 字段：响应不是 JSON（网关 502 的 HTML 等）时返回 undefined，
// 让 check 判失败，而不是把整个迭代打断成脚本错误。
function jsonField(response, path) {
  try {
    return response.json(path);
  } catch (err) {
    return undefined;
  }
}

export default function () {
  const ts = new Date().toISOString();
  // 每轮一个唯一事实：召回必须能把它原样带回来，否则"写进去了但召不回"。
  const fact = `k6事实-${__VU}-${__ITER}`;
  const append = http.post(
    `${BASE_URL}/memory/append`,
    JSON.stringify({
      request_id: `k6-${__VU}-${__ITER}`,
      user_id: "k6-user",
      session_id: "k6-session",
      round_id: `k6-round-${__VU}-${__ITER}`,
      messages: [
        { message_id: "m1", role: "user", content: `压测消息：我的编号是 ${fact}`, timestamp: ts },
        { message_id: "m2", role: "assistant", content: "好的，已记住", timestamp: ts },
      ],
      source_timestamp: ts,
    }),
    { headers: headers(__ITER), tags: { op: "append" } },
  );
  check(append, { "append 2xx": (r) => r.status >= 200 && r.status < 300 });

  // 写入是否真的落盘：后台任务必须跑到终态且 completed。
  const taskId = append.status === 200 ? jsonField(append, "task_id") : null;
  const task = taskId ? pollTask(taskId, __ITER) : null;
  check(task, { "append task completed": (t) => t !== null && t.status === "completed" });

  const recall = http.post(
    `${BASE_URL}/memory/recall`,
    JSON.stringify({
      user_id: "k6-user",
      session_id: "k6-session",
      query: fact,
    }),
    { headers: headers(__ITER), tags: { op: "recall" } },
  );
  check(recall, { "recall 2xx": (r) => r.status >= 200 && r.status < 300 });
  // 降级召回 = L3 后端不可用，只回了 L1/L2 兜底：算功能故障，不算通过。
  check(recall, { "recall 未降级": (r) => jsonField(r, "degraded") === false });
  check(recall, { "recall 命中本轮事实": (r) => recalledText(r).indexOf(fact) !== -1 });
  sleep(0.2);
}

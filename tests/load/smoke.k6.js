// k6 冒烟压测：真实后端（内网免鉴权，/v2/memory/* 为唯一业务面，不进 make check）。
//
//   make load-smoke BASE_URL=http://127.0.0.1:7002
//
// 被测服务要求：单 IP 限流调高（MEMORY_RATE_LIMIT_PER_MINUTE=600000、
// MEMORY_RATE_LIMIT_BURST=10000）。压测测的是服务容量，限流器另有一维。
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
// 每次运行一个唯一前缀：round_id 复用会命中幂等重试路径（那测的是
// 「重试对账」，不是「新写 + 召回」），测出来的是另一条路径的延迟。
const RUN_ID = __ENV.K6_RUN_ID || `run-${Date.now()}-${__VU}`;
// 后台任务轮询预算。L3 抽取要打 LLM，耗时由**模型端点**决定（实测本地
// 27B 端点在 4 路并发下单轮要 70s+），不是服务自己的延迟。所以这里给足
// 预算：这条检查要回答的是「写入最终落盘了吗」，不是「模型多快」。
// 请求延迟（append / recall）另有阈值管。
const TASK_POLL_MAX = 300;
const TASK_POLL_INTERVAL = 0.5;
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
    "Idempotency-Key": `k6-${RUN_ID}-${__VU}-${iter}`,
  };
}

// 轮询后台任务到终态；返回任务对象，未进入终态返回 null。
function pollTask(taskId, iter) {
  for (let i = 0; i < TASK_POLL_MAX; i++) {
    const res = http.get(`${BASE_URL}/v2/memory/tasks/${encodeURIComponent(taskId)}`, {
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

// 标记匹配：模型改写句子时可能改大小写或动尾巴，所以按**头部标记**匹配
// （头部是 k6cat<轮次>- ，每轮唯一），并忽略大小写。
function containsToken(text, token) {
  return String(text).toLowerCase().indexOf(String(token).toLowerCase()) !== -1;
}

// 长期记忆（L3）里是否召回了这个唯一标记。
// 只看「有没有召回到」会被 L2 拼接摘要兜底：L2 里就有本轮原文，所以哪怕
// L3 压根没写进去，检查也会过 —— 那正是「写进去了但召不回来」的反面
// （没写进去却看起来召回了）。必须单独盯 L3。
function recalledInL3(response, token) {
  const items = jsonField(response, "items");
  if (!Array.isArray(items)) {
    return false;
  }
  return items.some(
    (item) => item.layer === "L3" && containsToken(item.content || "", token),
  );
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
  // 每轮一个独立用户 + 一个唯一标记。
  // - 独立用户：共用一个用户时，后端抽取会把后面几轮判定成「已知」
  //   （既有记忆 + 最近消息都在提示同一件事）→ 抽取返回空 → L3 压根没记忆，
  //   校验会名存实亡。真实用法也是每个终端用户各自一份记忆。
  // - 唯一标记放句首：模型改写句子时改的是尾巴，头部标记能原样留下。
  const token = `k6cat${__ITER}-${RUN_ID.replace(/[^0-9]/g, "")}-${__VU}`;
  // 匹配用头部标记：模型改写时改的是尾巴，头部每轮唯一。
  const tokenHead = `k6cat${__ITER}-`;
  const fact = `我家猫的名字叫 ${token}`;
  const userId = `k6-user-${RUN_ID}-${__VU}-${__ITER}`;
  const sessionId = `k6-session-${RUN_ID}-${__VU}-${__ITER}`;
  const append = http.post(
    `${BASE_URL}/v2/memory/events`,
    JSON.stringify({
      request_id: `k6-${RUN_ID}-${__VU}-${__ITER}`,
      user_id: userId,
      session_id: sessionId,
      event: {
        event_id: `k6-round-${RUN_ID}-${__VU}-${__ITER}`,
        source: "chat_round",
        role: "user",
        payload: fact,
        occurred_at: ts,
      },
    }),
    { headers: headers(__ITER), tags: { op: "append" } },
  );
  check(append, { "append 2xx": (r) => r.status >= 200 && r.status < 300 });

  // 写入是否真的落盘：后台任务必须跑到终态且 completed。
  const taskId = append.status === 200 ? jsonField(append, "task_id") : null;
  const task = taskId ? pollTask(taskId, __ITER) : null;
  check(task, { "append task completed": (t) => t !== null && t.status === "completed" });

  const recall = http.post(
    `${BASE_URL}/v2/memory/recall`,
    JSON.stringify({
      user_id: userId,
      session_id: sessionId,
      query: "我家猫的名字叫什么",
    }),
    { headers: headers(__ITER), tags: { op: "recall" } },
  );
  check(recall, { "recall 2xx": (r) => r.status >= 200 && r.status < 300 });
  // 降级召回 = L3 后端不可用，只回了 L1/L2 兜底：算功能故障，不算通过。
  check(recall, { "recall 未降级": (r) => jsonField(r, "degraded") === false });
  check(recall, { "recall 命中本轮事实": (r) => containsToken(recalledText(r), tokenHead) });
  check(recall, { "recall 命中 L3 长期记忆": (r) => recalledInL3(r, tokenHead) });
  sleep(0.2);
}

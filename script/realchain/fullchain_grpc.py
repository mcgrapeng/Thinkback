"""innies-memory gRPC 全链路真实测试（内嵌 server 127.0.0.1:50052）。"""

from __future__ import annotations

import sys
import time
import uuid

import grpc

sys.path.insert(0, "src")
from innies_memory.rpc import memory_pb2 as pb  # noqa: E402
from innies_memory.rpc import memory_pb2_grpc  # noqa: E402

RUN = uuid.uuid4().hex[:8]
USER = f"grpcchain-{RUN}"
SESSION = f"session-{RUN}"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASSED if condition else FAILED).append(name if condition else f"{name}: {detail}")
    print(("  PASS " if condition else "  FAIL ") + name + ("" if condition else f" -> {detail}"))


def wait_task(stub, task_id: str, timeout: float = 420.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = stub.GetTask(pb.GetTaskRequest(task_id=task_id), timeout=10)
            status_name = pb.TaskStatus.Name(resp.status).lower().removeprefix("task_status_")
            if status_name in {"completed", "failed", "dead_letter"}:
                return status_name
        except grpc.RpcError as exc:
            if exc.code() == grpc.StatusCode.NOT_FOUND:
                pass
            else:
                raise
        time.sleep(2)
    return "timeout"


def main() -> int:
    channel = grpc.insecure_channel(
        "127.0.0.1:50052",
        options=[("grpc.enable_http_proxy", 0)],  # 内网直连禁用代理路由
    )
    grpc.channel_ready_future(channel).result(timeout=10)
    stub = memory_pb2_grpc.MemoryServiceStub(channel)
    print(f"== gRPC 全链路用户: {USER} ==")

    # 1. Append 两轮
    rounds = [
        (f"{RUN}-r1", 1, "我养了一只狗，名字叫旺财，金毛。", "旺财很乖！"),
        (f"{RUN}-r2", 2, "我最喜欢喝的美式咖啡，拿铁不爱喝。", "记住了，美式咖啡。"),
    ]
    task_ids = []
    for rid, idx, user_text, assistant_text in rounds:
        resp = stub.Append(
            pb.AppendRequest(
                request_id=f"req-{rid}",
                user_id=USER,
                session_id=SESSION,
                round_id=rid,
                round_index=idx,
                messages=[
                    pb.MemoryMessage(
                        message_id=f"{rid}-u",
                        role=pb.MESSAGE_ROLE_USER,
                        content=user_text,
                        timestamp="2026-09-07T10:00:00+00:00",
                    ),
                    pb.MemoryMessage(
                        message_id=f"{rid}-a",
                        role=pb.MESSAGE_ROLE_ASSISTANT,
                        content=assistant_text,
                        timestamp="2026-09-07T10:00:01+00:00",
                    ),
                ],
                source_timestamp="2026-09-07T10:00:01+00:00",
            ),
            timeout=30,
        )
        check(f"Append[{rid}] 200", resp.status in {"completed", "already_done"}, resp.status)
        task_ids.append(resp.task_id)
        again = stub.Append(
            pb.AppendRequest(
                request_id=f"req-{rid}-retry",
                user_id=USER,
                session_id=SESSION,
                round_id=rid,
                round_index=idx,
                messages=[
                    pb.MemoryMessage(
                        message_id=f"{rid}-u",
                        role=pb.MESSAGE_ROLE_USER,
                        content=user_text,
                        timestamp="2026-09-07T10:00:00+00:00",
                    ),
                    pb.MemoryMessage(
                        message_id=f"{rid}-a",
                        role=pb.MESSAGE_ROLE_ASSISTANT,
                        content=assistant_text,
                        timestamp="2026-09-07T10:00:01+00:00",
                    ),
                ],
                source_timestamp="2026-09-07T10:00:01+00:00",
            ),
            timeout=30,
        )
        check(f"Append[{rid}] 幂等 already_done", again.status == "already_done", again.status)

    # 2. L3 任务完成
    for (rid, *_), task_id in zip(rounds, task_ids, strict=True):
        status = wait_task(stub, task_id)
        check(f"GetTask[{rid}] L3 completed", status == "completed", status)

    # 3. 状态接口
    bg = stub.GetL3BackgroundStatus(pb.L3BackgroundStatusRequest(), timeout=10)
    check("GetL3BackgroundStatus", bg.write_mode in {"sync", "async"}, bg.write_mode)

    # 4. ListMemories / GetMemory
    listed = stub.ListMemories(
        pb.ListMemoriesRequest(user_id=USER, include_deleted=False), timeout=30
    )
    contents = [item.content for item in listed.items]
    print(f"  items={len(contents)}: {contents}")
    check("ListMemories 至少 1 条", len(contents) >= 1, str(contents))
    has_dog = any("旺财" in text for text in contents)
    print(f"  [提示] 狗事实入索引: {has_dog}")

    if listed.items:
        got = stub.GetMemory(
            pb.GetMemoryRequest(user_id=USER, memory_id=listed.items[0].memory_id), timeout=30
        )
        check("GetMemory 200", got.memory.memory_id == listed.items[0].memory_id, "id 不匹配")

    # 5. Recall
    recall_resp = stub.Recall(
        pb.RecallRequest(
            user_id=USER,
            session_id=SESSION,
            query="我的狗叫什么名字？",
            intent=pb.RECALL_INTENT_CHAT,
            l3_limit=8,
            l3_score_threshold=0.3,
            token_budget=2000,
        ),
        timeout=30,
    )
    joined = " ".join(item.content for item in recall_resp.items)
    check(
        "Recall 命中『旺财』", "旺财" in joined, f"items={[i.content for i in recall_resp.items]}"
    )

    # 6. Update（需要 operation_id）
    if listed.items:
        target = listed.items[0]
        upd = stub.UpdateMemory(
            pb.UpdateMemoryRequest(
                request_id=f"req-upd-{RUN}",
                user_id=USER,
                memory_id=target.memory_id,
                operation_id=f"op-upd-{RUN}",
                content=target.content + "（gRPC 确认）",
            ),
            timeout=60,
        )
        check(
            "UpdateMemory 200", upd.status in {"completed", "running", "already_done"}, upd.status
        )
        upd_status = wait_task(stub, upd.task_id, timeout=120)
        check("Update 任务完成", upd_status == "completed", upd_status)

    # 7. Delete + 幂等 + 删除后不泄漏
    op = f"op-del-{RUN}"
    dele = stub.Delete(
        pb.DeleteRequest(
            request_id=f"req-del-{RUN}",
            user_id=USER,
            scope=pb.DELETE_SCOPE_SESSION,
            operation_id=op,
            session_id=SESSION,
        ),
        timeout=60,
    )
    check("Delete 200", bool(dele.task_id), str(dele))
    del_status = wait_task(stub, dele.task_id, timeout=300)
    check("Delete 任务 completed", del_status == "completed", del_status)
    again = stub.Delete(
        pb.DeleteRequest(
            request_id=f"req-del2-{RUN}",
            user_id=USER,
            scope=pb.DELETE_SCOPE_SESSION,
            operation_id=op,
            session_id=SESSION,
        ),
        timeout=60,
    )
    check("Delete 幂等", again.status in {"already_done", "completed"}, again.status)

    after = stub.Recall(
        pb.RecallRequest(
            user_id=USER,
            session_id=SESSION,
            query="我的狗叫什么名字？",
            intent=pb.RECALL_INTENT_CHAT,
            l3_limit=8,
        ),
        timeout=30,
    )
    l3_leak = [
        item.content for item in after.items if item.layer == "L3" and "旺财" in item.content
    ]
    check("删除后 L3 不泄漏『旺财』", not l3_leak, str(l3_leak))

    # 8. Sensitive intent fail-closed
    try:
        stub.Recall(
            pb.RecallRequest(
                user_id=USER,
                session_id=SESSION,
                query="secret",
                intent=pb.RECALL_INTENT_SENSITIVE,
            ),
            timeout=10,
        )
        check("Sensitive fail-closed", False, "未拒绝")
    except grpc.RpcError as exc:
        check(
            "Sensitive fail-closed",
            exc.code() == grpc.StatusCode.PERMISSION_DENIED,
            str(exc.code()),
        )

    channel.close()
    print(f"\n== gRPC 结果: {len(PASSED)} passed / {len(FAILED)} failed ==")
    for failure in FAILED:
        print(f"  FAILED: {failure}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

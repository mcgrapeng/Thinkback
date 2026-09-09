"""Embedded gRPC server lifecycle.

提供两种使用方式：
- ``create_server``：构造并绑定一个 ``grpc.Server``，但不启动，常用于
  FastAPI lifespan 中嵌入到主进程。
- ``serve``：阻塞式启动，独立进程方式（``python -m innies_memory.rpc.server``），
  内置 SIGTERM/SIGINT 优雅退出。

反射（gRPC reflection）默认开启，方便 ``grpcurl`` 调试。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import grpc
from grpc_reflection.v1alpha import reflection
from loguru import logger

from innies_memory.memory.service import MemoryService
from innies_memory.rpc import memory_pb2, memory_pb2_grpc
from innies_memory.rpc.servicer import MemoryServicer


def create_server(
    service: MemoryService,
    *,
    host: str = "0.0.0.0",
    port: int = 50051,
    max_workers: int = 8,
    max_concurrent_rpcs: int | None = None,
) -> tuple[grpc.Server, str]:
    """创建并绑定 gRPC server，但**不**启动。

    返回 ``(server, address)``，调用方负责 ``start()`` 和 ``stop()``。
    启用 reflection 以便 grpcurl 等工具动态发现服务。
    """

    logger.bind(
        host=host,
        port=port,
        max_workers=max_workers,
        max_concurrent_rpcs=max_concurrent_rpcs,
    ).debug("grpc server creation started")
    server = grpc.server(
        ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="innies-grpc"),
        maximum_concurrent_rpcs=max_concurrent_rpcs,
    )
    memory_pb2_grpc.add_MemoryServiceServicer_to_server(MemoryServicer(service), server)  # type: ignore[no-untyped-call]
    reflection.enable_server_reflection(
        [
            memory_pb2.DESCRIPTOR.services_by_name["MemoryService"].full_name,
            reflection.SERVICE_NAME,
        ],
        server,
    )
    address = f"{host}:{port}"
    server.add_insecure_port(address)
    logger.bind(address=address).debug("grpc server bound")
    return server, address


def serve(
    service: MemoryService,
    *,
    host: str = "0.0.0.0",
    port: int = 50051,
    max_workers: int = 8,
    max_concurrent_rpcs: int | None = None,
    shutdown_grace_seconds: float = 5.0,
) -> None:
    """阻塞式启动 gRPC server（独立进程模式）。"""

    import signal

    server, address = create_server(
        service,
        host=host,
        port=port,
        max_workers=max_workers,
        max_concurrent_rpcs=max_concurrent_rpcs,
    )
    server.start()
    logger.info("grpc server started", address=address)

    def _shutdown(signum: int, _frame: object) -> None:
        """SIGTERM/SIGINT 处理：先 graceful stop，再由 ``wait_for_termination`` 返回。"""

        logger.info("grpc server shutting down", signal=signum)
        server.stop(grace=shutdown_grace_seconds)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)
    server.wait_for_termination()
    logger.info("grpc server stopped")

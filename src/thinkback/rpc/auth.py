"""gRPC 侧的 API Key 鉴权 —— 与 HTTP 同一份判定。

为什么必须有：gRPC 端口（k8s Service 暴露 50051）此前**完全没有鉴权** ——
``add_insecure_port`` 且没有任何 interceptor，任何能连到端口的调用方都能读写
任意 ``user_id`` 的记忆。HTTP 侧有 API Key + 租户隔离，gRPC 侧一条都没有。

口径（与 HTTP ``api/auth.py`` 一致）：

- metadata 里带 ``authorization: Bearer tbk_{env}_{key_id}_{secret}``；
- 密钥校验走 :func:`thinkback.api.auth.authenticate_api_key`（同一份实现，
  不在 gRPC 侧重写一遍哈希比对）；
- 租户以 **key 绑定的 tenant** 为准；客户端若额外带 ``x-tenant-id``，
  必须与 key 的 tenant 一致，否则 PERMISSION_DENIED（防「拿 A 租户的 key
  声明 B 租户」这种混淆）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import grpc
from loguru import logger

from thinkback.api.auth import ApiKey, ApiKeyError, authenticate_api_key

AUTHORIZATION_METADATA_KEY = "authorization"
TENANT_METADATA_KEY = "x-tenant-id"


def metadata_value(metadata: Iterable[Any] | None, key: str) -> str:
    """从 gRPC metadata 里取一个值（大小写不敏感）。

    metadata 元素有两种形态：interceptor 里是 ``(key, value)`` 元组，
    ``ServicerContext.invocation_metadata()`` 里是带 ``.key``/``.value``
    的具名元组 —— 两种都支持。
    """

    if not metadata:
        return ""
    for item in metadata:
        if isinstance(item, (tuple, list)) and len(item) >= 2:
            item_key, item_value = str(item[0]), str(item[1])
        else:
            item_key = str(getattr(item, "key", ""))
            item_value = str(getattr(item, "value", ""))
        if item_key.lower() == key.lower():
            return item_value
    return ""


def authenticate_metadata(metadata: Iterable[Any] | None) -> ApiKey:
    """校验一次 gRPC 调用的 API Key，返回 key 记录。

    失败抛 :class:`ApiKeyError`：401 → UNAUTHENTICATED，403 → PERMISSION_DENIED。
    """

    authorization = metadata_value(metadata, AUTHORIZATION_METADATA_KEY)
    record = authenticate_api_key(authorization)

    declared_tenant = metadata_value(metadata, TENANT_METADATA_KEY)
    if declared_tenant and declared_tenant != record.tenant_id:
        raise ApiKeyError("Tenant ID mismatch", status_code=403, code="TB-1003")

    return record


def grpc_status_for(exc: ApiKeyError) -> grpc.StatusCode:
    """把鉴权失败映射到 gRPC 状态码。"""

    if exc.status_code == 403:
        return grpc.StatusCode.PERMISSION_DENIED
    return grpc.StatusCode.UNAUTHENTICATED


def log_auth_rejection(exc: ApiKeyError, *, rpc: str) -> None:
    """统一的鉴权失败日志（不含 key_id / secret）。"""

    logger.bind(rpc=rpc, error_code=exc.code).warning("grpc auth rejected")


class ApiKeyAuthInterceptor(grpc.ServerInterceptor):  # type: ignore[misc]
    """gRPC 鉴权拦截器：没有合法 API Key 的调用一律拒绝。

    拦截点放在 handler 之前 —— 鉴权失败时业务代码一行都不跑。
    拒绝方式是「返回一个只会 abort 的 handler」：``ServicerContext.abort``
    会把指定的状态码带回客户端（UNAUTHENTICATED / PERMISSION_DENIED），
    直接抛异常只会得到 UNKNOWN。
    """

    def intercept_service(
        self,
        continuation: Any,
        handler_call_details: Any,
    ) -> Any:
        try:
            authenticate_metadata(handler_call_details.invocation_metadata)
        except ApiKeyError as exc:
            log_auth_rejection(exc, rpc=str(handler_call_details.method))
            handler = continuation(handler_call_details)
            if handler is None or not handler.unary_unary:
                # 未知方法 / 流式方法：交给 grpc 自己报 UNIMPLEMENTED，
                # 但鉴权失败仍然不能放行 —— 用 unary handler 兜底 abort。
                return _abort_handler(None, grpc_status_for(exc), exc.message)
            return _abort_handler(handler, grpc_status_for(exc), exc.message)
        return continuation(handler_call_details)


def _abort_handler(handler: Any, code: grpc.StatusCode, details: str) -> Any:
    """构造一个只会 abort 的 unary handler。"""

    def _abort(request: Any, context: Any) -> None:  # noqa: ARG001
        context.abort(code, details)

    return grpc.unary_unary_rpc_method_handler(
        _abort,
        request_deserializer=getattr(handler, "request_deserializer", None),
        response_serializer=getattr(handler, "response_serializer", None),
    )


__all__ = [
    "AUTHORIZATION_METADATA_KEY",
    "TENANT_METADATA_KEY",
    "ApiKeyAuthInterceptor",
    "authenticate_metadata",
    "grpc_status_for",
    "log_auth_rejection",
    "metadata_value",
]

"""租户命名空间 —— 把 tenant 带进存储层的唯一口径。

为什么需要它：``tenant_id`` 曾经只活在鉴权层（``api/auth.py`` 里校验一下就丢掉），
存储层完全不知道租户是什么 —— 所有查询都只按请求体里的 ``user_id`` 走。
于是「API Key 绑定了 tenant」这件事是装饰品：拿到别人的 key_id 就能读别人租户的数据。

为什么用命名空间而不是加一列：服务从未上线，没有存量数据要迁移；
而仓储协议有 44 个方法，加一列意味着每个查询都要多带一个维度、两个 adapter 都要改。
把 tenant 编进 user_id，隔离就变成「查询条件本来就带 user_id」这件事的自然结果，
不需要存储层知道租户的存在。

内部路由（``/memory/*``、``/admin/api/*``、gRPC）是 trusted admin 语义，
**不**做命名空间 —— 治理台需要按原始 user_id 看到全部数据。
"""

from __future__ import annotations

TENANT_USER_SEPARATOR = ":"


def scoped_user_id(tenant_id: str, user_id: str) -> str:
    """把租户命名空间加到 user_id 上。

    >>> scoped_user_id("tenant_a", "alice")
    'tenant_a:alice'
    >>> scoped_user_id("", "alice")
    'alice'

    tenant 里不允许出现分隔符：否则 ``acme:prod + alice`` 与 ``acme + prod:alice``
    会拼出同一个主体，造成跨租户读写。
    """
    if TENANT_USER_SEPARATOR in tenant_id:
        raise ValueError(f"tenant_id must not contain {TENANT_USER_SEPARATOR!r}")
    if not tenant_id:
        return user_id
    if not user_id:
        return tenant_id
    return f"{tenant_id}{TENANT_USER_SEPARATOR}{user_id}"


def tenant_of_scoped_user_id(scoped: str) -> str:
    """从命名空间 user_id 里取回租户；没有命名空间前缀时返回空串。

    治理台按 user_id 聚合时用它把 ``tenant:user`` 还原成可读的两段。
    """
    head, sep, tail = scoped.partition(TENANT_USER_SEPARATOR)
    if not sep or not head or not tail:
        return ""
    return head

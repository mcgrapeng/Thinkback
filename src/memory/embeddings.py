"""Mem0 embedding adapters for innies-memory.

承接 mem0 ``EmbedderFactory`` 的自定义 embedder provider 注册。
默认 provider 由 ``INNIES_OPENAI_EMBEDDER_CLASS`` 常量指向本文件中的
``OpenAICompatibleEmbeddingNoDimensions`` 类，作用是：
- 适配公司内部 embedding 网关（不走 OpenAI 官方 ``dimensions`` 参数）；
- 把 mem0 调用的输入归一化为单字符串，避免 OpenAI 多输入接口的并发抖动。
"""

from __future__ import annotations

from typing import Literal

from loguru import logger
from mem0.configs.embeddings.base import BaseEmbedderConfig
from mem0.embeddings.base import EmbeddingBase
from openai import OpenAI


class OpenAICompatibleEmbeddingNoDimensions(EmbeddingBase):  # type: ignore[misc]
    """OpenAI 兼容 embedder（不携带 ``dimensions`` 字段）。

    部分内部 embedding 服务（如自部署 vLLM、TEI 网关）会拒绝 ``dimensions`` 参数
    或只支持特定维度。该实现通过固定 ``embedding_dims`` + 客户端不传 ``dimensions``
    的方式兼容这类端点，同时与 mem0 的 collection 维度保持一致。
    """

    def __init__(self, config: BaseEmbedderConfig | None = None) -> None:
        super().__init__(config)
        self.config.model = self.config.model or "zhiman-embedding"
        self.config.embedding_dims = self.config.embedding_dims or 1024
        api_key = self.config.api_key or "not-required"
        base_url = self.config.openai_base_url or "http://embedding.example.internal:7345/v1"
        self.client = OpenAI(api_key=api_key, base_url=base_url)
        logger.bind(
            model=self.config.model,
            embedding_dims=self.config.embedding_dims,
            base_url=base_url,
        ).info("openai compatible embedder initialized")

    def embed(
        self,
        text: str | list[str],
        memory_action: Literal["add", "search", "update"] | None = None,
    ) -> list[float] | list[list[float]]:
        """把文本编码为 ``embedding_dims`` 维向量；兼容 mem0 的批量调用。

        mem0 的 UPDATE 事件路径会传入 ``list[str]``（旧记忆 + 新记忆
        一起重编码）并期望返回 ``list[list[float]]`` —— 单 str 签名会抛
        ``'list' object has no attribute 'replace'`` 并让整轮 L3 写入失败。
        与 mem0 官方 OpenAI embedder 保持同构：str 进 str 出、list 进 list 出。

        ``memory_action`` 仅为 mem0 协议占位参数，本实现不区分 add/search/update，
        因为底层 embedding 模型是相同的。
        """
        _ = memory_action
        if isinstance(text, list):
            normalized_batch = [item.replace("\n", " ") for item in text]
            response = self.client.embeddings.create(
                input=normalized_batch,
                model=self.config.model,
            )
            by_index = {item.index: item.embedding for item in response.data}
            return [list(by_index[index]) for index in range(len(normalized_batch))]
        normalized_text = text.replace("\n", " ")
        response = self.client.embeddings.create(
            input=[normalized_text],
            model=self.config.model,
        )
        return list(response.data[0].embedding)

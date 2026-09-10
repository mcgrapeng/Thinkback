"""OpenAI-compatible 同步 LLM 客户端（供 L2 综合摘要等应用层调用）。

与 mem0 内部的 LLM 无关——这是应用层自有的轻量调用通道：
复用 ``MEMORY_LLM_*`` 配置（base_url/model/api_key），同步阻塞式，
调用方（L2 后台刷新器）已在工作线程中执行，不阻塞事件循环。
"""

from __future__ import annotations

from typing import Any

from loguru import logger


class OpenAICompatibleLLMClient:
    """最小封装：``complete(system, user) -> text``，异常统一包装为 RuntimeError。"""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout_seconds: float = 30.0,
        max_tokens: int = 512,
        client: Any | None = None,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.max_tokens = max_tokens
        self._client = client

    def _borrow_client(self) -> Any:
        if self._client is not None:
            return self._client
        from openai import OpenAI

        self._client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )
        return self._client

    def complete(self, system: str, user: str) -> str:
        """执行一次 chat completion；任何失败包装为 RuntimeError 上抛。"""

        call_log = logger.bind(model=self.model, system_length=len(system), user_length=len(user))
        call_log.debug("llm completion started")
        try:
            response = self._borrow_client().chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=self.max_tokens,
                timeout=self.timeout_seconds,
            )
            text = str(response.choices[0].message.content or "").strip()
        except Exception as exc:
            call_log.bind(error_type=type(exc).__name__).warning("llm completion failed")
            raise RuntimeError(f"llm completion failed: {exc}") from exc
        if not text:
            raise RuntimeError("llm completion returned empty content")
        call_log.bind(completion_length=len(text)).debug("llm completion completed")
        return text

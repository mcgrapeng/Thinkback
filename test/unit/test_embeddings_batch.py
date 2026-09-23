"""R-6 回归：embedder 必须兼容 mem0 UPDATE 路径的批量调用。

真实全链路（real_mem0_pressure）第 3 轮 UPDATE 事件中 mem0 传入
list[str] 重编码，单 str 签名抛 'list' object has no attribute 'replace'，
导致整轮 L3 写入失败。
"""

from __future__ import annotations

from typing import Any

from thinkback.memory.embeddings import OpenAICompatibleEmbeddingNoDimensions


class _FakeEmbeddings:
    def __init__(self) -> None:
        self.inputs: list[Any] = []

    def create(self, input: list[str], model: str) -> Any:  # noqa: A002
        self.inputs.append(list(input))

        class _Item:
            def __init__(self, index: int, value: str) -> None:
                self.index = index
                self.embedding = [float(len(value)), 1.0]

        class _Resp:
            data = [_Item(i, v) for i, v in enumerate(input)]

        return _Resp()


class _FakeClient:
    def __init__(self) -> None:
        self.embeddings = _FakeEmbeddings()


def _make_embedder() -> tuple[OpenAICompatibleEmbeddingNoDimensions, _FakeClient]:
    embedder = OpenAICompatibleEmbeddingNoDimensions.__new__(OpenAICompatibleEmbeddingNoDimensions)

    class _Cfg:
        model = "zhiman-embedding"
        embedding_dims = 1024

    embedder.config = _Cfg()
    embedder.client = _FakeClient()
    return embedder, embedder.client


def test_embed_accepts_single_string() -> None:
    embedder, client = _make_embedder()
    result = embedder.embed("麻薯\n很可爱")
    assert result == [6.0, 1.0]
    assert client.embeddings.inputs == [["麻薯 很可爱"]]


def test_embed_accepts_batch_list_for_mem0_update_path() -> None:
    """mem0 UPDATE 会传 list[str] 并期望 list[list[float]]（按输入顺序）。"""
    embedder, client = _make_embedder()
    result = embedder.embed(["旧记忆", "新\n记忆"])
    assert result == [[3.0, 1.0], [4.0, 1.0]]
    assert client.embeddings.inputs == [["旧记忆", "新 记忆"]]

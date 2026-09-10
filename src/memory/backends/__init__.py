"""L3 长期记忆后端包。

``MemoryBackend`` 协议已下沉到 ``innies_memory.domain.ports``；
本包提供两个实现：``fake``（测试）与 ``mem0_library``（生产）。
旧 ``innies_memory.memory.backends`` 模块的公开名保持 re-export 兼容。
"""

from innies_memory.domain.ports import MemoryBackend
from innies_memory.memory.backends.fake import FakeMemoryBackend
from innies_memory.memory.backends.mem0_library import (
    INNIES_OPENAI_EMBEDDER_CLASS,
    Mem0LibraryMemoryBackend,
    build_mem0_library_config,
    disable_mem0_telemetry,
    register_mem0_embedder_provider,
)

__all__ = [
    "FakeMemoryBackend",
    "INNIES_OPENAI_EMBEDDER_CLASS",
    "Mem0LibraryMemoryBackend",
    "MemoryBackend",
    "build_mem0_library_config",
    "disable_mem0_telemetry",
    "register_mem0_embedder_provider",
]

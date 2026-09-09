"""纯领域层（domain）。

只依赖标准库，不依赖任何框架、存储或传输层：
- ``enums``: 领域状态机与词表枚举
- ``entities``: 持久化无关的领域实体（Journal/Summary/MemoryIndex/Task）
- ``keys``: 幂等指纹与作用域键
- ``summarization``: L2 摘要策略
- ``safety``: 内容安全准入策略
- ``recall_policy``: 召回去重与预算裁剪策略
- ``slots``: P0 槽位抽取引擎（正则 NLP）
- ``errors``: 类型化领域错误
- ``ports``: 仓储 / 后端 / 历史源端口协议

依赖方向（只允许向内）：
``api / rpc -> memory(application) -> infra -> domain``
"""

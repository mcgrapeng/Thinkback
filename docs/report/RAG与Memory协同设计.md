# RAG与Memory协同设计

| 元信息项 | 当前值 |
| --- | --- |
| 文档责任人 | 张朋 |
| 适用系统 | `thinkback` + `thinkback` 对话服务 |
| 文档类型 | 架构设计说明 |
| 维护范围 | RAG与Memory职责边界、记忆过期处理策略、业界实践参考 |

本文说明 Thinkback 企业知识搜索与AI对话平台中，RAG（检索增强生成）与Memory（记忆系统）的协同设计原则，解决"RAG知识更新后Memory是否过期"的架构问题。

---

## 1. 核心问题陈述

### 1.1 业务场景

Thinkback 是面向半导体行业的企业知识搜索与AI对话平台，具备以下特性：

- **RAG开关**：用户可开启/关闭知识库检索
- **三层记忆**：L1（会话上下文）、L2（会话摘要）、L3（长期记忆）
- **知识更新**：半导体工艺文档、设备手册会定期更新

### 1.2 架构疑问

当RAG知识库更新后（如"7nm工艺良率从85%更新到92%"），Memory中的相关记忆（如"用户认为良率偏低"）是否会过期？是否需要清理或重新验证？

### 1.3 本文结论

**RAG与Memory职责不同，更新周期不同，不存在"过期"问题**。关键在于：

1. Memory **不应该** 缓存RAG的知识结论
2. Memory **只应该** 记录用户与知识的交互轨迹
3. 召回时动态组合两者，由LLM生成时处理版本差异

---

## 2. 职责边界

### 2.1 RAG的职责：静态领域知识

```
存储内容：
  - 半导体工艺文档（7nm制程标准、良率规范）
  - 设备手册（光刻机参数、蚀刻设备配置）
  - 行业标准（SEMI标准、ISO规范）

更新周期：周/月级别（文档版本发布）

作用域：租户级或部门级共享

典型查询："7nm工艺的标准流程是什么？"
```

### 2.2 Memory的职责：动态用户状态

```
存储内容：
  - 用户画像（角色：工艺工程师，负责28nm产线）
  - 交互历史（上次询问7nm良率问题，关注成本优化）
  - 用户偏好（习惯先看流程图再看参数表）
  - 待办事项（等待设备X参数确认，跟进良率异常）

更新周期：实时（每轮对话后）

作用域：user_id级隔离

典型查询："这个用户最近关注什么？上次聊到哪了？"
```

### 2.3 职责分离原则

| 维度 | RAG | Memory |
|------|-----|--------|
| **本质** | 知识库 | 状态机 |
| **类比** | 图书馆（存书） | 读者笔记（存阅读记录） |
| **存什么** | 客观事实、技术规范 | 主观状态、交互轨迹 |
| **谁消费** | 所有用户 | 单个用户 |
| **过期逻辑** | 文档版本替换 | TTL + 重建 |

**金科玉律**：
- ✅ Memory记录："用户询问过7nm良率，关注点是成本优化"
- ❌ Memory不记录："7nm工艺良率标准是92%"

---

## 3. 过期问题的本质分析

### 3.1 伪过期场景（错误设计）

```python
# ❌ 错误的Memory设计：缓存了RAG的结论
memory_item = {"content": "7nm工艺良率偏低（85%），建议优化", "created_at": "2024-01-15"}

# RAG文档更新后
rag_doc_v2 = "7nm工艺良率已提升至92%"

# 问题：Memory中的"85%"和"偏低"评价已过期
```

**为什么会出现这种设计？**
- 将Memory当作"对话内容缓存"而非"用户状态记录"
- 混淆了"用户说了什么"和"用户关心什么"

### 3.2 正确设计（不存在过期）

```python
# ✅ 正确的Memory设计：只记录用户状态
memory_item = {
    "content": "用户询问7nm工艺良率问题，关注成本优化方向",
    "metadata": {
        "topic": "7nm_yield",
        "user_concern": "cost_optimization",
        "interaction_count": 3,
        "last_discussed": "2024-01-15",
    },
}

# RAG文档更新后
rag_doc_v2 = "7nm工艺良率已提升至92%（2024-02-01更新）"

# Memory仍然有效：
# - "用户关注7nm良率"依然成立
# - "关注点是成本"依然成立
# - RAG提供最新数据，Memory提供用户上下文
```

**召回时的组合策略**：

```python
def generate_response(query: str, user_id: str):
    # 1. 召回用户Memory（用户状态，永远有效）
    user_context = memory.recall(user_id, query)
    # → "用户是工艺工程师，最近3次询问7nm良率，关注成本优化"

    # 2. 检索最新RAG知识（领域知识，永远最新）
    rag_results = rag.search(query)
    # → "7nm工艺良率：92%（2024-02-01数据）"

    # 3. LLM组合生成
    prompt = f"""
    用户背景：{user_context}
    最新知识：{rag_results}

    用户提问：{query}

    请结合用户背景回答。如果知识有更新，主动说明变化。
    """

    return llm.generate(prompt)


# 生成结果示例：
# "根据最新数据（2024-02-01），7nm工艺良率已提升至92%，
#  相比您上次询问时有显著改善。从成本优化角度看，良率提升
#  可减少废片率约15%，预计单片成本下降..."
```

---

## 4. Memory提取规则

### 4.1 提取决策树

```
对话内容
    ↓
是否包含用户个人信息？
    ├─ 是 → 提取到 L3 (PROFILE)
    │      示例："我是28nm产线的工艺工程师"
    │
    └─ 否 → 是否体现用户偏好？
            ├─ 是 → 提取到 L3 (PREFERENCE)
            │      示例："我习惯先看流程图再看参数"
            │
            └─ 否 → 是否是待办事项？
                    ├─ 是 → 提取到 L2/L3 (PLAN)
                    │      示例："帮我跟进良率异常问题"
                    │
                    └─ 否 → 是否是交互关注点？
                            ├─ 是 → 提取到 L2 (EVENT)
                            │      示例："询问7nm良率，关注成本"
                            │
                            └─ 否 → 不提取
                                   示例："7nm良率标准是..." (RAG内容)
```

### 4.2 提取白名单（应该进Memory）

| 类型 | 示例 | 记忆层级 |
|------|------|----------|
| **用户身份** | "我是工艺工程师，负责28nm产线" | L3 PROFILE |
| **用户偏好** | "我喜欢先看流程图再看参数表" | L3 PREFERENCE |
| **待办事项** | "帮我持续跟进EUV对准精度问题" | L2/L3 PLAN |
| **关注点追踪** | "用户连续3次询问7nm良率，关注成本" | L2 EVENT |
| **交互模式** | "用户倾向于用图表理解数据" | L3 PREFERENCE |

### 4.3 提取黑名单（不应进Memory）

| 类型 | 示例 | 应该存在哪里 |
|------|------|-------------|
| **技术知识** | "7nm工艺标准流程包括..." | RAG文档 |
| **设备参数** | "ASML光刻机EUV波长13.5nm" | RAG文档 |
| **文档引用** | "根据SEMI标准，良率要求..." | RAG文档 |
| **计算结果** | "良率92%意味着废片率8%" | 临时计算，不持久化 |

### 4.4 实现示例

```python
def should_extract_to_memory(
    message: dict, role: str, conversation_context: dict
) -> tuple[bool, str | None]:
    """
    判断消息是否应提取为Memory

    Returns:
        (是否提取, 记忆类型)
    """
    content = message["content"]

    # 用户消息：提取意图和状态
    if role == "user":
        # 身份声明
        if any(pattern in content for pattern in ["我是", "我负责", "我的职位", "我在"]):
            return (True, "PROFILE")

        # 偏好表达
        if any(pattern in content for pattern in ["我习惯", "我喜欢", "我倾向", "我一般"]):
            return (True, "PREFERENCE")

        # 待办请求
        if any(pattern in content for pattern in ["帮我跟进", "持续关注", "记得提醒", "需要追踪"]):
            return (True, "PLAN")

        # 询问关注点（提取topic，不提取答案）
        if is_question(content):
            return (True, "EVENT")

    # 助手消息：仅提取对用户状态的理解
    elif role == "assistant":
        # 不提取技术知识回答
        if contains_rag_citation(content):
            return (False, None)

        # 提取对用户意图的确认
        if any(pattern in content for pattern in ["您是在问", "您关心的是", "理解您的需求"]):
            return (True, "EVENT")

    return (False, None)


def preprocess_for_memory(messages: list) -> list:
    """
    预处理对话，过滤掉不应进Memory的内容
    """
    filtered = []
    for msg in messages:
        should_extract, mem_type = should_extract_to_memory(
            msg, msg["role"], conversation_context={}
        )

        if should_extract:
            # 去除RAG引用标记
            content = remove_citations(msg["content"])

            # 如果是助手消息，只保留意图理解部分
            if msg["role"] == "assistant":
                content = extract_user_intent_understanding(content)

            filtered.append({"role": msg["role"], "content": content, "memory_type": mem_type})

    return filtered
```

---

## 5. L2摘要设计原则

### 5.1 当前实现（V1）

```python
# V1: 拼接最近10轮user消息
l2_content = "；".join([round.user_message for round in recent_10_rounds])
```

**评价**：
- ✅ 正确：只保留用户消息，不缓存助手回答中的RAG内容
- ✅ 简单：无需LLM压缩，延迟低
- ⚠️ 局限：无语义压缩，token消耗较高

### 5.2 升级方向（V2）

```python
# V2: LLM压缩为结构化摘要
summarize_prompt = """
请总结用户在本会话中的状态变化，输出JSON格式。

**必须包含**：
1. topics: 用户关注的主题列表
2. pending_tasks: 待跟进事项
3. behavior_patterns: 观察到的交互偏好

**严格禁止**：
- 不要总结技术知识内容本身
- 不要包含具体数值、参数、规范
- 不要复述助手的回答内容

输入对话：
{recent_10_rounds}

输出示例：
{{
    "topics": ["7nm工艺良率", "成本优化方向", "EUV对准精度"],
    "pending_tasks": ["等待设备X参数确认", "下周跟进良率异常"],
    "behavior_patterns": ["习惯先看流程图", "倾向于对比历史数据"]
}}
"""

l2_summary = llm.generate(summarize_prompt)
```

**关键约束**：
- ✅ 提取："用户连续3次询问7nm良率"
- ❌ 禁止："7nm良率标准是92%"

---

## 6. RAG版本追踪策略

### 6.1 问题场景

```
时刻T1: 用户询问"7nm良率多少？"
        RAG返回：doc-7nm-spec-v2.3 → "良率85%"
        Memory记录："用户询问7nm良率"

时刻T2: RAG文档更新
        doc-7nm-spec-v3.1 → "良率92%"

时刻T3: 用户继续询问"7nm良率有改善吗？"
        如何让LLM知道"上次是v2.3的85%，现在是v3.1的92%"？
```

### 6.2 解决方案：元数据追踪

#### 扩展Memory元数据

```python
# ins_memory 表扩展
memory_item = {
    "memory_id": "mem-123",
    "content": "用户询问7nm工艺良率问题，关注成本优化",
    "metadata": {
        "topic": "7nm_yield",
        "user_concern": "cost_optimization",
        "interaction_count": 3,
        # 🔑 新增：追踪用户交互时引用的RAG文档版本
        "rag_context": {
            "doc_id": "doc-7nm-spec",
            "version": "v2.3",
            "timestamp": "2024-01-15T10:00:00Z",
            "key_data_points": ["良率85%"],  # 可选：记录用户关注的数据点
        },
    },
    "created_at": "2024-01-15T10:00:00Z",
}
```

#### 召回时版本对比

```python
def recall_with_version_awareness(query: str, user_id: str):
    # 1. 召回Memory
    memory_items = memory.recall(user_id, query)

    # 2. 检索最新RAG
    rag_results = rag.search(query)
    current_doc = rag_results[0]

    # 3. 检测版本差异
    version_changes = []
    for mem in memory_items:
        if "rag_context" in mem.metadata:
            old_version = mem.metadata["rag_context"]["version"]
            if old_version != current_doc.version:
                version_changes.append(
                    {
                        "topic": mem.metadata["topic"],
                        "old_version": old_version,
                        "new_version": current_doc.version,
                        "old_timestamp": mem.metadata["rag_context"]["timestamp"],
                        "new_timestamp": current_doc.updated_at,
                    }
                )

    # 4. 构建prompt
    prompt = f"""
    用户背景：{memory_items}

    最新知识：{rag_results}

    版本变化提示：
    {format_version_changes(version_changes)}

    用户提问：{query}

    请主动说明知识更新，并结合用户历史关注点回答。
    """

    return llm.generate(prompt)


def format_version_changes(changes: list) -> str:
    """格式化版本变化提示"""
    if not changes:
        return "无版本变化"

    lines = ["检测到知识更新："]
    for c in changes:
        lines.append(
            f"- {c['topic']}: {c['old_version']} ({c['old_timestamp']}) "
            f"→ {c['new_version']} ({c['new_timestamp']})"
        )
    return "\n".join(lines)
```

#### 生成效果示例

```
用户问："7nm良率现在怎么样了？"

LLM生成：
"根据最新数据（v3.1，2024-02-01更新），7nm工艺良率已提升至92%。
相比您上次询问时（v2.3，2024-01-15）的85%，有显著改善。

从您关注的成本优化角度看，良率提升7个百分点意味着：
- 废片率从15%降至8%
- 预计单片成本下降约12%
- ..."
```

---

## 7. 业界实践参考

### 7.1 主流产品对比

| 产品 | RAG能力 | Memory能力 | 是否混淆 |
|------|---------|-----------|---------|
| **ChatGPT** | 无企业RAG（纯对话） | 个人Memory（偏好、事实） | 否 |
| **Microsoft 365 Copilot** | SharePoint文档、邮件 | 用户工作习惯、常用模板 | 否 |
| **Notion AI** | Notion页面知识库 | 用户最近编辑、偏好 | 否 |
| **Perplexity** | 实时网页检索 | 搜索历史、关注主题 | 否 |
| **Claude Projects** | 项目知识库（文档上传） | 项目上下文、对话历史 | 否 |

**共同特征**：
1. **严格分离**：没有任何产品让Memory缓存RAG内容
2. **版本感知**：检索时使用最新RAG，Memory提供用户上下文
3. **智能组合**：由LLM在生成时动态融合两者

### 7.2 Mem0的设计理念

Mem0（thinkback当前使用的L3后端）的核心约束：

```python
# Mem0自动提取的记忆类型
class MemoryType(Enum):
    PROFILE = "用户画像"  # "我是工艺工程师"
    PREFERENCE = "用户偏好"  # "我喜欢看流程图"
    EVENT = "交互事件"  # "用户询问过7nm良率"
    PLAN = "待办事项"  # "跟进设备参数确认"


# Mem0 **拒绝** 提取的内容
rejected_patterns = [
    "技术规范定义",  # "7nm工艺标准是..."
    "设备参数数值",  # "光刻机波长13.5nm"
    "文档引用内容",  # "根据SEMI标准..."
]
```

**Mem0的提取prompt**（简化版）：

```
从对话中提取关于**用户**的事实、偏好和事件。

提取规则：
✅ 提取：用户的身份、偏好、关注点、待办事项
❌ 不提取：技术知识、文档内容、计算结果

示例：
对话："我是28nm产线工程师，最近在研究7nm良率问题。
      根据文档，7nm良率标准是92%。"

提取：
- PROFILE: "用户是28nm产线工程师"
- EVENT: "用户正在研究7nm良率问题"

不提取：
- "7nm良率标准是92%" ❌（这是技术知识，应存在RAG）
```

---

## 8. 实施检查清单

### 8.1 Memory提取验证

- [ ] Memory中不存在RAG文档的技术内容
- [ ] Memory中不存在具体数值、参数、规范
- [ ] Memory只记录用户身份、偏好、关注点、待办
- [ ] 可以通过Memory回答"用户最近关注什么"
- [ ] 不能通过Memory回答"7nm良率标准是多少"

### 8.2 召回逻辑验证

- [ ] 召回时并行查询Memory和RAG
- [ ] Memory召回基于用户状态（user_id + session_id）
- [ ] RAG召回基于语义相似度（query embedding）
- [ ] 组合时Memory提供用户上下文，RAG提供最新知识
- [ ] LLM生成时可感知版本变化

### 8.3 过期处理验证

- [ ] RAG文档更新后，Memory不需要批量失效
- [ ] Memory的TTL基于时间（如30天），与RAG更新无关
- [ ] L2摘要标脏（DIRTY）基于删除操作，不基于RAG更新
- [ ] 召回时通过元数据追踪检测版本差异
- [ ] LLM可主动说明"数据较上次有更新"

---

## 9. 常见反模式

### 反模式1：Memory当作对话缓存

```python
# ❌ 错误：把完整对话内容存入Memory
memory.add(
    {"user": "7nm良率多少？", "assistant": "根据最新文档，7nm工艺良率为92%，符合SEMI标准..."}
)

# ✅ 正确：只提取用户状态
memory.add({"event": "用户询问7nm良率", "topic": "7nm_yield", "user_concern": "符合标准"})
```

### 反模式2：Memory与RAG冗余存储

```python
# ❌ 错误：在Memory中复制RAG内容
memory.add({"content": "7nm工艺标准流程：光刻→蚀刻→沉积→..."})

# ✅ 正确：RAG存流程，Memory存用户关注点
rag.index({"doc_id": "7nm-process", "content": "7nm工艺标准流程：光刻→蚀刻→沉积→..."})
memory.add({"event": "用户查阅过7nm工艺流程文档", "focus": "光刻环节"})
```

### 反模式3：RAG更新触发Memory清理

```python
# ❌ 错误：RAG文档更新后删除相关Memory
def on_rag_document_updated(doc_id: str):
    related_memories = memory.search(doc_id)
    for mem in related_memories:
        memory.delete(mem.id)  # 错误：用户状态不应因知识更新而失效


# ✅ 正确：RAG更新不影响Memory，召回时动态组合
def on_rag_document_updated(doc_id: str):
    # 什么也不做，或者记录更新日志供版本追踪
    log_rag_update(doc_id, new_version)
```

---

## 10. 总结

### 核心原则

```
┌─────────────────────────────────────────────────────┐
│  原则1：职责分离                                      │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   │
│  RAG  = 知识库（存客观事实）                         │
│  Memory = 状态机（存用户轨迹）                       │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  原则2：Memory不缓存知识                              │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   │
│  ✅ 记录："用户询问过X，关注Y"                      │
│  ❌ 不记录："X的技术定义是Z"                        │
└─────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────┐
│  原则3：动态组合，不预判过期                          │
│  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━   │
│  召回时：Memory（用户上下文） + RAG（最新知识）      │
│  生成时：LLM感知版本差异并主动说明                   │
└─────────────────────────────────────────────────────┘
```

### 实施路径

1. **当前（V1）**：确保Memory提取规则不包含RAG内容
2. **近期（V1.1）**：在Memory元数据中追踪RAG版本
3. **中期（V2）**：升级L2摘要为LLM压缩，强化"只存状态"约束
4. **长期**：基于真实流量优化版本追踪策略

### 验收标准

可以通过以下测试验证设计正确性：

```python
# 测试1：Memory不包含RAG内容
assert "7nm良率是92%" not in memory.list_all(user_id)

# 测试2：RAG更新不触发Memory失效
old_memory_count = len(memory.list_all(user_id))
rag.update_document("doc-7nm-spec", new_version="v3.1")
new_memory_count = len(memory.list_all(user_id))
assert old_memory_count == new_memory_count

# 测试3：召回可感知版本变化
response = recall_and_generate(query="7nm良率现在怎么样？", user_id="user-123")
assert "较上次" in response or "已更新" in response
```

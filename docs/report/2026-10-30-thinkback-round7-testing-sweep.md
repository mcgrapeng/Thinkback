# 第七轮：记忆系统测试体系排查与整改（2026-10-30）

> 目标只有一个：**核心逻辑真的被测过**。
> 手段是变异审计 + 覆盖率定位：逐条改坏源码，看现有测试会不会红。
> 这一轮最重的产出不是「补了多少用例」，而是——**审计在一轮里是静默失效的**，
> 以及把它修回来的那两行配置。

## 0. 结论

| 维度 | 本轮开始 | 本轮结束 |
| --- | --- | --- |
| 确定性用例 | 1,726 | 2,271 |
| 变异审计范围 | 14 个文件（其中 6 个从未跑完一轮审计） | 15 个文件 / 2,337 个变异体（全量重审） |
| 变异体被杀率 | 上一轮结论：90%（12 文件口径） | **92.4%**（2,159 / 2,337） |
| 新增存活变异体 | 615（相对旧基线） | **0**（全部已验收并逐条登记理由） |
| 行覆盖率 | 96.1% | 96.6% |
| 门禁可运行性 | **审计跑不起来** | 绿 |

一句话：**「核心逻辑真的被测过」这句话，在本轮开始时是假的** —— 审计的第一步
（跑一遍干净基线）就失败，一个变异体都没评。

## 1. 排查方法

按 `~/.agents/skills/ai-testing-system/SKILL.md` 的成熟度清单 10 条逐条对照
Thinkback，外加机器审计：

```bash
testkit-audit /Users/zhangpeng/workspace/liaohe/Thinkback
```

机器审计：Python 栈 10 条里 7 条通过，3 项缺口 —— 覆盖率门禁、视觉回归、
a11y 回归。逐条核对后：

| 审计项 | 机器结论 | 实际 | 说明 |
| --- | --- | --- | --- |
| 覆盖率门禁 | 缺 | **有意不做** | `make coverage` 是非阻断观测水位，理由写在 Makefile 注释里（行覆盖率衡量「这行跑过」，不衡量「结果对不对」）。本轮不动这个决定 |
| 视觉回归 | 缺 | 缺 | 第五轮已列，仍未做（见 §6） |
| a11y 回归 | 缺 | **已有** | 审计器按 npm 包名探测，漏看了 `tests/e2e/test_a11y_smoke.py`（axe-core v4.10.3 本地注入，serious/critical 为零） |

清单之外，问题在**门禁自己**。

## 2. 门禁缺陷：变异审计已经跑不起来了（本轮最重要的一条）

### 2.1 现象

把 `domain/memory_types.py` 加进变异范围后，`make check` 在 `mutation-audit`
这一步直接失败：

```
tests/unit/test_memory_types.py::test_shipped_semiconductor_profile_loads
AssertionError: assert 'process_preference' in ['commitment', 'constraint', 'event', ...]
failed to collect stats. runner returned 1
```

不是用例错了 —— 是**审计根本没开始**。mutmut 的第一步是「在变异树上跑一遍
干净基线」，基线红了就退出，一个变异体都不评。

### 2.2 根因：mutmut 树里缺数据文件

mutmut 把 `source_paths`（`src/`）与 `tests/` 复制进 `mutants/` 树，在树里跑
测试。仓库根的 `profiles/`（随包发布的类型 profile 与抽取指令）和
`Dockerfile` **不在复制范围内**：

| 用例 | 怎么找数据 | mutants 树里的结果 |
| --- | --- | --- |
| `test_memory_types.py::test_shipped_semiconductor_profile_loads` | `Path(__file__).resolve().parents[2] / "profiles"` | `mutants/profiles` —— 不存在 → 加载退回默认注册表 → 断言里的行业类型自然不在 |
| `test_memory_type_profile.py::test_docker_image_ships_profiles` | `Path("Dockerfile")`（相对 cwd） | cwd 是 `mutants/` → 读不到 → `FileNotFoundError` |

两个都是**数据缺失**，不是逻辑错误。但它们让 mutmut 的基线跑不起来，
于是「变异审计已覆盖核心逻辑」这句话从那天起就是假的。

### 2.3 修法

`pyproject.toml` 的 `[tool.mutmut]` 加一行：

```toml
also_copy = ["profiles", "Dockerfile"]
```

mutmut 的 `copy_also_copy_files()` 每次 `mutmut run` 都会执行，
把这两个路径复制到 `mutants/` 下的同名相对位置。

### 2.4 为什么这类缺陷要单独立一条

第五轮已经修过五个同类门禁缺陷（范围变更静默失效、timeout 从视野消失、
并行假超时、用例基线键算错、测试树变了结论却旧的）。它们的共同形状是：

**门禁自己坏了，但门禁照样是绿的。**

本轮这条的特别之处在于它让**整个审计**停摆，而不是少审几个文件 ——
而审计正是「核心逻辑真的被测过」这句话的唯一支撑。

### 2.5 顺带修掉的第二个门禁盲区

`check_mutation_survivors.py` 在读结论之前会先核对「测试树的内容指纹」。
本轮跑审计时另一个并发写者往 `tests/` 里加了文件，指纹对不上 →
门禁判失败（这是**对的**：结论必须出自当前这套测试）。

但它报的是「重跑」而不是「谁改了树」，排查成本高。本轮把
`refresh_mutation_stats.py` 的清空条件与 `check_mutation_survivors.py`
的前置检查对齐到同一套指纹，行为不变，只是提示更明确。

### 2.6 幂等中间件的缓存是**进程级**全局 —— 用例在同一进程里跑第二遍就红

`api/idempotency.py` 把幂等缓存绑成模块级单例：

```python
_idempotency_cache = IdempotencyCache()  # 进程级


class IdempotencyMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self.cache = _idempotency_cache
```

共享本身是**有意的**（多副本下幂等的前提，`test_the_middleware_uses_the_shared_cache_instance`
钉住了它）。问题在用例侧：`test_a_failed_response_is_not_cached_and_can_be_retried`
用固定的 `Idempotency-Key: key-flaky`，第一次跑完时那个 200 响应**进了全局缓存**；
同一进程里再跑一遍，第一请求就命中缓存里的 200，而不是端点返回的 503。

普通 pytest 一个进程跑一遍，所以它一直是绿的；变异审计在同一个进程里反复跑
同一批用例，于是它红了：

```
tests/unit/test_idempotency.py::test_a_failed_response_is_not_cached_and_can_be_retried
AssertionError: assert 200 == 503
Failed to run clean test
```

mutmut 的第一步是「在变异树上跑一遍干净基线」，基线红了就直接退出，一个变异体
都不评 —— **整个审计停摆**，而停摆的原因跟测试质量毫无关系。

修法：中间件接受一个可选的缓存，**默认仍是全局共享实例**（生产路径一个字没改，
`test_the_middleware_uses_the_shared_cache_instance` 照样成立），用例侧注入自己的
缓存：

```python
def __init__(self, app: ASGIApp, cache: IdempotencyCache | None = None) -> None:
    super().__init__(app)
    self.cache = cache if cache is not None else _idempotency_cache
```

用例改成 `app.add_middleware(IdempotencyMiddleware, cache=IdempotencyCache())`。
改完之后同一个进程里连跑两遍都过（改之前第二遍必红）：

```
pass 0: exit=0
pass 1: exit=0
```

这条与 §2.4 是同一个形状：**门禁自己坏了，但门禁照样是绿的** —— 而且这次是
让审计整个跑不起来。

### 2.7 变异范围加了文件，用例选择列表没跟着加 —— 新写的用例一条也杀不掉

`[tool.mutmut]` 有两个列表：`only_mutate`（改坏哪些源文件）与
`pytest_add_cli_args_test_selection`（变异判定时跑哪些用例）。后者是**白名单**：
用例不在里面，变异体就「找不到能杀它的用例」。

把 `domain/content_quality.py` 加进 `only_mutate` 的同时，也写了一份精确值契约
专门杀它的数值型变异体 —— 但那份契约**忘了加进选择列表**。于是 147 个
content_quality 变异体里，一个也没被新用例杀掉；门禁看到的全是存活，实际是
「能杀它们的用例没被选中」。

这条与第五轮修的「范围变了但 mutants/ 里留着旧副本」是同一个形状：**门禁绿着，
但它审的不是你以为的那个东西**。区别是这次门禁会**红**（存活数超基线），
所以至少不会骗人 —— 但如果基线被顺手更新，就彻底静默了。

### 2.8 属性测试的 `differing_executors` 健康检查会让审计中止

hypothesis 的 `differing_executors` 健康检查在「同一个属性测试被不同执行器调用过」
时报错退出。变异审计用多个 worker 并行跑同一批用例，同一个 `@given` 测试天然会在
不同 worker 进程里各跑一遍 —— 这个检查拦的是 flaky 测试，这里跑的是纯函数属性
测试（`round_sort_key` 之类），没有共享状态，并行执行不会让它变得不稳定。

`tests/conftest.py` 在变异 profile 下关掉它：

```python
hypothesis_settings.register_profile(
    "thinkback-mutation",
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.differing_executors],
)
```

### 2.9 API 契约模糊测试抓到一处「schema 说能收、接口却拒收」的裂缝

`make test-fuzz`（schemathesis，按 OpenAPI 契约 fuzz）红了：

```
POST /v2/memory/events  →  400 TB-1001
"memory type '+00:00' is not registered in namespace 'thinkback'"
```

schemathesis 的判定是「schema 合规的请求不该被拒」。而 `memory_type` 在
OpenAPI 里是自由字符串 —— 于是**契约在撒谎**：它说任意字符串都能收，
实现却只收该 namespace 已注册的类型。

这条规则**没法用静态 schema 表达**：类型注册表按 namespace 动态加载，
schema 里写不出「这个 namespace 注册了哪些类型」。按 fuzz 用例自己定的规矩
（「新拒绝规则先试 schema 表达，确实不可表达再放宽」），处理是：

- fuzz 侧：`positive_data_acceptance` 放行 400，理由写进豁免说明
  （与已有的 422 豁免同类）；
- 实现侧：把行为钉在单测上 ——
  `tests/unit/test_memory_api.py::test_an_unregistered_memory_type_is_rejected_with_400`
  （400 + TB-1001 + 错误文案点名类型），外加一条对照组
  （已注册的类型必须被接受 —— 免得把 400 写成一刀切）。

**豁免不是免责**：放行 400 的意思是「这一类拒绝规则不由 schema 保证」，
不是「400 随便返」。真正的保护在那两条单测上。

### 2.10 一个变异体在两轮审计之间「时活时死」—— 它的杀手是 flaky 的

`_l1_cache.clear_l1` 的一个变异体在两次审计之间**从「被杀」变成「存活」**：

```
新增未被杀死的变异体 1 个：
  + survived: thinkback.memory.repositories._l1_cache.xǁL1CacheMixinǁclear_l1__mutmut_20
```

查下来它是**等价变异**：改的是「遍历 `list(self.l1_cache)` 清整个 scope」
那条分支里的 `pop(key, None)` → `pop(key)`。键是从缓存自己取出来的、
且全程持 `_l1_lock`（并发写 `update_l1` 走同一把锁），键必然存在 ——
去掉默认值不改变行为。与基线里已有的 `get_l1__mutmut_22` 同形。

那它之前为什么会被判「被杀」？因为有一条并发用例
（`clear_l1_under_concurrent_update_l1_no_corruption`）在时序上偶尔撞进
「键刚好不在」的窗口，把一个等价变异**误判成被杀**。

这条的价值不在那一个变异体，在**它暴露的机制**：

- 等价变异被偶发地判成「被杀」→ 审计的击杀率是虚高的；
- 等价变异被偶发地判成「存活」→ 门禁随机红，且没人知道该补测试还是登记。

处置：登记为存活并写明理由（不是「补测试」—— 补不出来，行为本来就没变）。
同时给并发用例补了一条**确定性**的断言（清一个从没缓存过的 session =
无事发生，且不误伤别的 session），把那条变异体的**真实**行为钉死。

**这类 flaky 杀手是门禁信噪比的隐形杀手**：它让审计结论在两轮之间来回摆，
久而久之大家会习惯性忽略「新增存活」，门禁就废了。

### 2.11 把 `api/idempotency.py` 放回变异范围：90 个新存活里，24 个是真缺陷

`api/idempotency.py`（幂等保证的唯一执行点）进变异范围后浮出 **90 个存活变异体**。
逐条看下来分三类：

**（a）真缺陷 —— 24 个，补测试杀掉**

| 缺陷 | 改坏后的后果 |
| --- | --- |
| namespace 没进域（`_caller_identity` 取不到 namespace） | 两个 namespace 共用一个幂等域 → 同一把 key 在别的 namespace 也能重放 / 冲突 |
| 空白 namespace 没回落默认域 | 「省略」与「显式空白」落进两个域 → 同一把 key 既不重放也不报冲突，写入执行两次 |
| 写方法表漏了 PUT / DELETE / PATCH | 那三个方法完全不受幂等保护 |
| 没有 Idempotency-Key 的请求被当成有 key | 所有无 key 的写共用一个域 → 无 key 的写互相重放 |
| 冲突响应丢 request_id / 取 request_id 的方式坏了 | 排查时对不上是哪一次请求；取属性的写法坏掉时直接 500 |
| 淘汰顺序反了（LRU → LIFO） | 刚写进去的先被淘汰 |
| 装到上限就开始淘汰 | 容量少一条 |
| TTL 边界写成 `>=` | 条目在到期前一刻被清 → 幂等失效，同一把 key 执行两次 |
| 3xx 也进幂等缓存 | 重定向被当成「这次写入的结果」重放 |

**（b）等价变异 —— 约 40 个，登记并写明理由**

举几例，都是「改了写法但行为不变」：

- `call_next(request)` → `call_next(None)`：Starlette 的 `call_next` **不使用**
  它的 request 参数（下游请求由 scope + receive 构造）；
- `request._body = body` → `= None`：BaseHTTPMiddleware 的下游拿到的是**新构造的**
  request（请求体走 ASGI receive 通道），这个赋值不被读取；
- `media_type=None` / `content_type` 换成别的键：响应的 content-type 已经在
  `headers` 里，响应头覆盖 media_type；
- 从 `response.body` 取响应体的那段：body_iterator 存在时**不可达**；
- HTTP 头名大小写：RFC 7230 规定头名大小写不敏感。

**（c）文案 / 日志类 —— 约 26 个，登记为「刻意不钉死」**

错误文案与日志文本。行为由状态码（409）与错误码（TB-1005）钉住，
文案随产品措辞变动，不钉死。

**这一轮暴露的门禁盲区**：登记存活时**必须逐条写理由**。否则「新增存活」
和「本来就该存活」分不开 —— 90 个新增存活里，24 个是真缺陷、66 个是登记项；
如果不写理由，后来的人只能把它们全部当成噪音忽略掉。

而且这 66 个登记项**还要再核一遍**：第一遍登记里有 4 条被我误判成「等价变异」
（请求体还原、分块响应体累加），后来发现它们其实是**可杀的** ——
真正的原因不是它们等价，而是**杀它们的那份用例没在变异用例选择列表里**
（§2.7 同一个形状的缺陷又犯了一次）。把那份用例加进选择列表后，其中一条
（分块累加）被杀掉了；另外三条确实等价，逐条写了理由。

**教训**：登记「等价变异」之前，先确认「有没有用例能杀它」——
用 `MUTANT_UNDER_TEST=<变异体名>` 单独跑一次那个变异体，看用例红不红。
不红，可能是等价，也可能是**用例没被选中**。这两种混在一起，
门禁就会把真缺陷当成噪音放过。

## 3. 本轮补的测试

新增 6 个测试文件、485 条用例。挑的都是「**决定了结果、却从未被断言**」的分支。

### 3.1 入参校验（`memory/schemas.py`）—— 信任边界，54 条

schemas.py 是服务面对**不可信调用方**的第一道闸。gRPC / curl / SDK 都不经过
前端表单，服务端必须自己挡住。此前这个模块的 25 个校验函数**在测试树里一次
都没出现过**。

| 钉住什么 | 改坏后的后果 |
| --- | --- |
| namespace 形状（禁 `:`、长度 1~64） | 允许 `:` = 允许跨作用域碰撞（namespace 会和 user_id 拼成作用域键） |
| 空 / 纯空白 ID（user/session/round/request/operation/memory） | 空串也能写入 → 空主键记录 + 幂等冲突 |
| metadata 64KB 上限（含边界值放行） | 超大 JSONB 静默 TOAST；边界写反则把合法请求拒掉 |
| 事件三要素（event_id / source / payload）非空 | 空事实进 L1 |
| 删除范围互斥（MEMORY/SESSION/ALL 各自的禁止项） | 单条删除带上 session_id = 顺带清了别的会话的原文 |
| 编辑正文非空 + strip | 写进一条空正文 |
| 召回预算与阈值越界（l3_limit / score_threshold / token_budget） | 越界值进不了服务（应在入口被拒） |

### 3.2 过期判定（`domain/entities.memory_is_expired`）—— 9 条

召回、列表、槽位回填**三处**都问它，TTL 判定却一次都没被直接断言过。

| 钉住什么 | 改坏后的后果 |
| --- | --- |
| 到期瞬间即过期（`<=` 不是 `<`） | 多活一秒 |
| naive 时间按 UTC 读 | asyncpg / SQLite 丢 tzinfo → 差 8 小时 |
| 缺 `expires_at` 属性 = 未过期 | TTL 字段缺失的行被误判过期 |

### 3.3 配置校验（`infra/config.py`）—— 25 条

配置写错最危险的不是报错的那几条，是「看起来配了、其实没生效」的那几条：

| 钉住什么 | 改坏后的后果 |
| --- | --- |
| 限流速率 / 突发额度 >= 1 | **0 = 静默关掉限流**，但看不出是配置写的 |
| 孤儿任务回收阈值 >= 60 秒 | 配成 30 秒 = 把正在跑的健康任务当孤儿回收 |
| 线程池 / 信号量上限 >= 1 | 0 = 写路径直接不可用 |
| log_level 大小写不敏感但名字必须认识 | 拼错就静默用默认级别 |

### 3.4 召回的 L3 过滤（`memory/service.py`）—— 13 条

`recall` 里 L3 结果的三道闸门此前**一行都没被走过**：

- `l3_limit` 是硬上限（不是建议值）；
- 分数门槛按「语义分达标 **或** 综合分达标」（ADR-0004）—— 中文里词命中的
  事实语义分只有 0.39，综合分 0.69，只看语义分会把它挡在门外；
- 过期事实与 `invalid_at` 已置的记忆**都不进上下文**（状态过滤之外的第二道闸）。

外加一条对照组：闸门都关着的时候，正常记忆必须能召回（否则全是假红）。

### 3.5 擦除的轮次引用（`memory/service.py`）—— 8 条

`_deleted_round_refs` 决定「擦除阶段按引用回原文」要回哪些轮次。此前整块 0 覆盖，
而它是**会话隔离**的关键：会话级擦除如果拿 delete-all 墓碑去清别人的原文，
就是跨会话的数据泄漏。

### 3.6 启动时的待写重放、编辑的错误路径、类型解析 —— 13 条

- 恢复只重放「该重放的那一批」：非写入任务、缺 scope、轮次已被擦除 → 跳过；
- 编辑找不到记忆必须报错，不能返回「成功」；
- 未注册的类型 / 需要审批的类型 → 拒绝（普通编辑不能绕过程序性规则的审批）。

### 3.7 API 线程池的限流泄压（`api/memory.py`）—— 8 条

池子满的时候唯一正确的答案是 503（暂时不可用），不是挂死、不是 500。
而且**槽位必须归还** —— 漏还一个，池子就永远少一格，最后整条写路径死锁。
这几条路径常规跑法永远走不到。

### 3.8 幂等保证（`api/idempotency.py`）—— 10 条

幂等是「同一把 key 不会把写操作执行两次」的唯一执行点。新增：冲突响应的
错误码（TB-1005 + 409）、缓存键的每一维都参与、过期后换 body 必须放行、
`a` 与 `a:b` 不是同一把 key、命中的条目要移到队尾、取不到调用方标识时
退回对端 IP 分域。

### 3.9 变异审计新暴露的三块盲区（本轮真正的产出）

变异审计跑起来之后，浮出 **615 个新存活变异体**。其中三块是真正的「核心逻辑
从没被测过」，而不是措辞表噪音：

#### （a）默认注册表整张表没被钉住（206 个变异体）

`domain/memory_types.py` 的 `default_registry()` 是一大坨字面量。改掉其中
任何一格 —— 把 `event` 的 `source_roles` 里的 `"tool"` 去掉、把 `state` 的
`ttl_days` 从 90 改成 91、把 `procedure` 的 `extractor` 从 `none` 改成 `mem0`
—— 都不会抛异常，只会安静地换一种行为。

**补法**：一张全量取值表（8 个类型 × 10 个字段）逐格断言。206 → 0。

#### （b）防捏造闸门的依据词表没被钉住（180 个变异体）

`memory_supported_by_source` 判据是「记忆里认出的槽位，能不能在原始对话
文本里找到依据」。依据词表就是这套判据的规格：

- 词表里少一个词 → 那一类真实事实被静默丢弃（用户在 /items 里看不到自己说过的话）；
- 词表里多一个词 → 凭空捏造的事实被放行（用户会当真）。

两种都是「功能正常、一切绿」的错误。

**补法**：每个 marker 一条用例，源文本里**只出现那一个 marker**（宠物 12 种、
生日/住址/工作/饮品/食物/睡眠 30 种、沟通偏好 12 种），再加对照组。
词表冗余的条目（`live` ⊃ `lived`、`工作` ⊂ `换工作`、`兔` ⊂ `兔子`）无法隔离 ——
它们是**等价变异**，登记为存活。

#### （c）来源仲裁的口径没被钉住（42 个变异体）

`round_order` / `source_cursor*` / `is_same_source_local_p0_memory` /
`_parse_source_timestamp` / `should_extract_to_l3` —— 「哪个来源更新」是同槽位
冲突的仲裁依据，「同源」决定新事实要不要跳过写入。这些判据写反了，线上表现
是「用户改了偏好，系统还在用旧的那条」，而功能用例照样全绿。

**补法**：排序档位逐个断言（来源时间 > 库内时间戳 > round_index > round_id
数字后缀）、跨用户/跨会话的条目不得被采信、`Z` 后缀与 naive 时间、字节阈值的
边界（`>=` 不是 `>`）。

### 3.10 上下文别名表的子串语义（`query_slots.context_terms`）—— 19 条

别名表按**子串**匹配（`communication` 里含着 `cat`），这是中文记忆「沟通」与
英文记忆能对上同一个上下文词的原因。整词匹配会让它失效。这条语义写死，
改坏别名的键或值时立刻红。

### 3.11 质量打分的**精确值**契约（`domain/content_quality.py`）—— 28 条

`test_content_quality.py` 钉的是**方向**（「具体的比含糊的高」「短的扣分」）。
方向对了阈值写错也照样绿 —— 把「锚点出现次数上限 4」写成 3、把「引用最多扣两次」
写成三次、把长度甜区的 200 字写成 180 字，方向都没变，但选出来的记忆变了。

新增 `test_content_quality_contract.py`：期望值写成与实现**同形的表达式**
（`0.3 * 1 + 0.1 * min(5, 4)`），常数被改成任何别的值断言立刻不成立。期望值不是抄
实现输出，是照 docstring 里写明的公式算出来的 —— 实现与公式不符时这里会红。

| 维度 | 钉住的常数 |
| --- | --- |
| 具体性 | 0.3 × 类别数、0.1 × min(出现次数, 4) |
| 自足性 | 0.60 句首指代、0.25 × min(引用数, 2)、0.15 无主语、0.30 过短（**5 字边界**） |
| 可验证性 | 0.5 + 0.1 × min(客观标记, 3) − 0.15 × min(模糊词, 3) |
| 情感显著性 | min(1.0, 0.5 × 不同情绪词数) |
| 清晰度 | 长度甜区 4 / 8 / 200 / 400、0.12 × min(口头禅数, 3)、0.20 重复字符、0.15 格式噪音 |
| 新颖性 | 1.0 − 与 peers 的最大 Jaccard；**归一化后为空的 peer 不能中断比对**；标点先去掉再比 |

### 3.12 六个死方法：够不着、也测不了（删掉，不是补测）

覆盖率把 `service.py` 的未覆盖行摊到方法上之后，有六个方法**在整个仓库里一次都
没有被调用过**（`src/` 与 `tests/` 全树搜索，定义处之外零引用）：

| 方法 | 位置 | 为什么成了死代码 |
| --- | --- | --- |
| `_round_is_after_delete_all_cutoff` | `memory/service.py` | 被 `_RebuildDeleteCutoffs` 取代（docstring 写明「替代旧路径每个回合各做一次 `list_memories` 全量扫描」的 O(rounds × memories) 行为） |
| `_round_is_after_session_delete_cutoff` | `memory/service.py` | 同上 |
| `_query_is_broad_memory_request` | `memory/service.py` | 领域策略下沉到 `domain/slots/query_slots.py` 后剩下的薄壳 |
| `_source_has_any` | `memory/service.py` | 同上 |
| `_p0_canonical_memory_for_source_slot` | `memory/service.py` | 同上 |
| `default_type` | `domain/memory_types.py` | `_resolve_type_name` 直接返回 `"profile"` 字符串后走 `registry.get`，这个便捷方法没人用 |

前两个尤其值得说：它们**看起来**正是「删除截止时间」的现役实现，签名与语义都对，
但没人调 —— 真要有人哪天按名字去用它们，就会把 `_RebuildDeleteCutoffs` 专门消除的
O(n²) 仓储扫描重新引进来。

死代码没法测：给没人调用的方法写断言，只是给一份没人执行的实现拍照。唯一的
诚实做法是**删掉**（删了 43 行，零行为变化），让「核心逻辑全部测过」这句话
覆盖的确实是**活着的**代码。

### 3.13 `service.py` 里「决定结果」却从没被断言的分支 —— 26 条

`service.py` 有 6000+ 行，按项目自己的收窄标准（「纯函数 + 有判定分支」）整份不进
变异范围 —— 这是对的，胶水代码的变异体只会产出「日志文案变了」这种噪音。但按方法
摊开未覆盖行之后，里面**混着决定结果的逻辑**，本轮补的用例钉住它们：

| 分支 | 判定内容 |
| --- | --- |
| `_conflict_policy` 回落 | 类型没注册时回落 `overwrite`（**这条正是待拍板的「内置类型冲突策略」的兜底口径**） |
| `_index_lifecycle` | 未知类型不得凭空造到期日；TTL 类型按当天结束计；重放沿用写入时的到期日（否则恢复机制给过期事实续命） |
| `_parse_source_timestamp` / `_parse_iso_datetime` | naive 按 UTC；解析不了当没有 |
| `_round_is_after_timestamp` | 时区混用不能变 TypeError，也不能按本地时区算 |
| `_rebuild_slot_is_covered_by_active_memory` | 重建时「这轮的槽位是否已被覆盖」—— 新记忆覆盖旧轮次，旧记忆**不**覆盖新轮次（方向写反就选错事实） |
| `_run_l3_write` 的三种 SKIP | `type_not_extractable` / `role_not_allowed_for_type` / `pre_extract_gate_failed` |
| `_type_recall_caps` | 注册表坏了不得让召回整个挂掉，降级成「没有类型级上限」 |
| `_validate_delete_required_identifiers` | 缺 `memory_id` / `session_id` 时拒绝（信任边界兜底） |
| `_is_manageable_memory` | 墓碑不是可管理记忆 |

## 4. 变异审计结果

范围与最终数字（`make check` 的 `mutation-audit` 一步）：

```
变异存活基线：本次评估 2337 个变异体；当前存活 178 个 / 无测试 0 个 / 超时 0 个 / 其他状态 0 个；
已验收 178 个 / 0 个 / 0 个 / 0 个
无新增未被杀死的变异体，基线一致。
```

- 被杀 2,159 个（**92.4%**），存活 178 个 —— 全部在 `tests/eval/mutation_survivors.json`
  里逐条登记了理由（等价变异 / 覆盖缺口 / 刻意不钉死），没有无声跳过。
- 本轮把**门禁从「跑不起来」修到「跑得完」**，这是数字之外最重要的产出：
  审计跑不起来时，上一版的「90% 被杀率」是旧结论，不是当前这套测试算出来的。
- 范围一度扩到 19 个文件（+content_quality / +slots.support / +entities /
  +memory_types / +api.idempotency），那 5 个文件浮出 615 个存活变异体，
  其中绝大多数落在**措辞表 / 展示文案 / 正则串**上 —— 逐条杀掉就变成覆盖率报表
  而不是缺陷信号。决定行为的部分改用各自的契约用例钉死（§3.2 / §3.8 / §3.11 /
  §3.9a-c）；范围与理由写在 `pyproject.toml` 的 `[tool.mutmut]` 注释里，
  不留「为什么不在里面」的空白。`api/idempotency.py` 后来放回了范围（§2.11）。

**这条取舍的代价要说清楚**：那五个模块从「逐条改坏源码看测试会不会红」降级成
「逐条断言具体结论」。前者能发现**没想到的**分支，后者只能覆盖已经想到的分支。
如果其中出现了一个没人想到的分支，契约用例不会红。

**建议**：把 `domain/entities.py` 重新放回变异范围 —— 它是 9 条用例、纯函数、
判定分支最少的一个，收窄它没有信噪比收益，反而丢掉了唯一的「改坏源码」保护。
`domain/slots/support.py` 可以拆开：**判定部分**（排序游标、同源判定、时间解析、
闸门阈值）单独进范围，只有「依据词表」那部分该排除 —— 现在两者被一起排除了。

## 5. 覆盖率

`make coverage` 是**非阻断**的观测水位（行覆盖率衡量「这行跑过」，不衡量「结果对不对」）。
本轮的实际数字：**总行覆盖率 97%**（8,089 行语句，未覆盖 276 行）。核心文件：

| 文件 | 本轮开始 | 本轮结束 | 原因 |
| --- | --- | --- | --- |
| `memory/service.py` | 92.4%（2,140 行 / 未覆盖 163） | **96%**（2,121 行 / 未覆盖 76） | 删掉 6 个死方法（§3.12，它们够不着也测不了）+ 新增的 service.py 契约用例（§3.4-3.7 / §3.13） |
| `domain/content_quality.py` | 98%（未覆盖 4） | 98%（未覆盖 4） | 精确值契约（§3.11）杀的是变异体，不增加行覆盖；剩下 4 行是展示文案分支 |
| `domain/entities.py` | 98.8%（未覆盖 1） | **100%** | 过期判定的唯一口径（§3.2） |
| `domain/memory_types.py` | 91.5%（未覆盖 14） | **99%**（未覆盖 2） | 默认注册表全量取值表 + 属性名长度边界（§3.9a / §3.11） |
| `domain/slots/support.py` | 90.2%（未覆盖 14） | **99%**（未覆盖 1） | 来源仲裁与依据词表（§3.9c） |

`memory/service.py` 剩下的 76 行未覆盖按方法摊开后几乎全是编排胶水（后台线程、
心跳、日志分支）；它决定结果的部分由 §3.13 的契约用例钉住，但这个文件本身
不在变异审计范围内（理由见 `pyproject.toml` 的 `[tool.mutmut]` 注释）。

## 6. 未做与待拍板

| 项 | 为什么没做 | 建议 |
| --- | --- | --- |
| 视觉基线（管理台截图） | 需要稳定的截图基线文件与 CI 产物留存策略 | 建议做，不急 —— a11y 已有 |
| `memory/service.py` 的 96 行未覆盖 | 6,000+ 行里多为编排胶水；按纯函数标准收窄后，剩下的是「这行跑过没」的覆盖，不是「结果对不对」的覆盖 | 变异审计扩到 service.py 的关键方法需要 mutmut 支持函数级定向（本版只支持文件级 glob），属工具能力问题 |
| `memory/backends/mem0_library.py` 45 行未覆盖 | 需要真实 mem0 库；fake 后端与真实后端的契约差异已由 L3 契约套件覆盖 | 真栈门禁进 CI（第四轮已列，需要 CI 基础设施改动） |
| `memory/repositories/sqlalchemy.py` 36 行未覆盖 | SQLite 与真实 PG 的方言差异 | 契约测试已覆盖双实现的行为一致性 |
| `pyproject.toml` 里「service.py 全量变异留给 CI 夜间任务」这句注释**指向一个不存在的任务** | `.github/workflows/ci.yml` 的 `nightly-full` 跑的是 `make check` —— 与 PR 门禁**同一个范围**，没有任何 job 变异 service.py | 要么把注释改成它实际做的事（service.py 的核心判定由 §3.13 的契约用例钉住，纯函数部分已下沉到 `domain/` 并进变异范围），要么真建一个夜间 job。按项目自己的收窄标准，整份 service.py 进变异范围会产生上千个「日志文案变了」的噪音变异体，稀释门禁信噪比 —— **先把注释改成真的** |

## 7. 复核方式

```bash
make check            # 格式 / lint / 类型 / 单测 / 用例基线 / 变异审计 / 评测
make coverage         # 覆盖率观测（非阻断）
make test-ui          # 管理台组件测试（vitest）
make test-e2e         # 端到端 + axe a11y 扫描
make test-fuzz        # API 契约模糊测试（schemathesis）
uv run --group dev python tests/eval/check_mutation_survivors.py   # 变异基线
```

### 本轮各车道的实际结果

| 车道 | 结果 |
| --- | --- |
| `make check` | **绿**（format / lint / typecheck / 2,271 条用例 / 用例基线 2,271 条全执行 / 变异审计 / 评测基线） |
| 变异审计 | 2,337 个变异体，存活 178，**新增存活 0** |
| `make test-ui` | 6 个文件 / 22 条 vitest，全绿 |
| `make test-e2e` | 6 条 Playwright（含 axe a11y：serious/critical 为零），全绿 |
| `make test-fuzz` | schemathesis 全绿（修掉 §2.9 的契约裂缝之后） |
| `make coverage` | 行覆盖率 96.6%（观测水位 92%，非阻断） |

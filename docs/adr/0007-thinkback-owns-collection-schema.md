# Milvus collection 由 Thinkback 预建，mem0 接管

Thinkback 自己创建并持有 Milvus collection 的 schema，mem0 发现 collection 已存在且字段齐全时接管读写。

理由：mem0 2.2.1 建 collection 时把 `text` 字段硬编码成不带 `analyzer_params` 的分析器，我们没有配置项能给中文分词器；自己预建才能带上中文分析器、BM25 Function、sparse 字段与两个索引。

代价：schema 归我们管，mem0 升级时它的建表逻辑变化不会自动生效，必须由我们的兼容测试兜住。collection 重建按新建加复制式回填执行，不重新抽取事实。

切换窗口采用双写：写同时进新旧两个集合，回滚只需把配置改回旧集合名，不需要补数据，回滚时数据缺口的风险消失。双读窗口 48 小时，召回读新集合、未命中回落旧集合，切换判据是新集合行数与旧集合一致且抽样召回通过。旧集合至少保留 30 天再清理。窗口名与回滚步骤写进运行手册。

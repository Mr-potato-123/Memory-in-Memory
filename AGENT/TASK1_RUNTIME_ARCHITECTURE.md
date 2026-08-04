# MiM Runtime Architecture

## Construction Agent 与 Access & Answer Agent 实现级设计

> 文档定位：`TASK1_RESULT.md` 的运行侧深化设计。  
> 目标：把“能演示 Agent 概念”的玩具实现，升级成可以真实跑 LoCoMo、支持版本更新、混合检索、时间问题和失败回放的最小可运行架构。  
> 范围：重点描述 Memory 如何表示、储存、更新、检索，以及 Construction / Access & Answer Agent 如何操作这些能力。

---

## 1. 最终技术决策

运行侧采用：

```text
SQLite
├─ 原始 conversation / session / message
├─ Construction commit
├─ Memory version
└─ FTS5 全文索引

本地 Embedding Model
└─ Memory content → float32 vector

NumPy
└─ 在当前 conversation 内执行向量相似度检索

Construction Agent
├─ 从 session 抽取候选 Memory
├─ 检索相关旧 Memory
├─ 决定 ADD / UPDATE / MERGE / SKIP
└─ 原子提交一个 session 的更新

Access & Answer Agent
├─ 根据问题选择检索策略
├─ 迭代 search / inspect
├─ 判断证据是否充分
└─ 在同一 loop 中输出 answer + evidence
```

选择 SQLite 而不是 JSON 作为主存储的原因：

- 可真实查询和过滤；
- session 更新可使用事务；
- 支持 FTS5；
- 可以重建任意 Construction commit 时的 Memory snapshot；
- 不需要单独数据库服务；
- Python 内置 `sqlite3`，部署成本很低；
- 当前项目环境已验证 SQLite 3.45.3 和 FTS5 可用。

JSON 不再作为主存储，只用于：

- 导出某个 snapshot 供人工检查；
- Skill Bank 版本；
- trace、failure 和评测结果。

---

## 2. 从现有 Baseline 保留和改进什么

### 2.1 保留 SimpleMem 的部分

`baseline/SimpleMem` 中值得保留：

1. 每条 Memory 是自包含、消除指代的完整表述；
2. Memory 同时具备：
   - 语义向量；
   - 关键词；
   - 时间、人物、实体等结构化字段；
3. Retrieval 同时考虑：
   - semantic；
   - keyword；
   - structured；
4. 对问题进行多轮检索，而不是固定一次 Top-k。

### 2.2 SimpleMem 当前不够的部分

为了 MiM 归因与回放，需要补上：

- 原始 source message ID；
- Memory 版本链；
- session 级事务；
- 当前时间与历史时间的区分；
- state change、correction、merge 的不同语义；
- 可重建 snapshot 的 commit；
- 稳定的多路检索融合分数；
- evidence 必须来自已检索结果；
- Base/MiM 统一的检索预算。

### 2.3 不采用 A-MEM 图链接作为 MVP 必选项

`baseline/A-mem` 会维护 Memory links 和邻居演化。这有研究价值，但不是当前 MVP 必需：

- 图链接增加 Construction 操作空间；
- 增加归因难度；
- LoCoMo 的多数问题可以由版本化 Memory + 多路检索处理；
- Access Agent 本身可以通过多轮 query 完成多跳检索。

第一版保留 `related_memory_ids` 字段，但不主动构建或遍历图。后续确有收益再启用。

---

## 3. 运行侧组件

```text
RuntimeWorkflow
├─ MessageRepository
├─ SQLiteMemoryStore
├─ EmbeddingModel
├─ HybridRetriever
├─ ConstructionAgent
├─ AccessAnswerAgent
├─ SkillBankReader
└─ TraceRecorder
```

职责边界：

| 组件 | 负责 | 不负责 |
| --- | --- | --- |
| MessageRepository | 保存原始 session/message | 不生成 Memory |
| SQLiteMemoryStore | Memory 版本、commit、事务、snapshot | 不做 LLM 决策 |
| EmbeddingModel | 文本向量化 | 不排序业务规则 |
| HybridRetriever | 多路召回、融合、过滤 | 不生成最终答案 |
| ConstructionAgent | 候选抽取、更新决策 | 不直接执行 SQL |
| AccessAnswerAgent | 迭代检索并回答 | 不修改 Memory |
| SkillBankReader | 检索并返回同侧 Skill | 不学习 Skill |
| TraceRecorder | 保存调用和证据轨迹 | 不参与决策 |

Agent 只输出结构化计划；Store 负责校验并执行。禁止让模型生成或执行 SQL。

---

## 4. 数据隔离

### 4.1 LoCoMo

每个 conversation 是独立 Memory namespace：

```text
conversation_id = LoCoMo sample/conversation ID
```

任何查询必须显式带 `conversation_id`。即使语义上高度相似，也不能跨 conversation 召回事实。

### 4.2 Run 隔离

每个评测 run 使用独立 SQLite 文件：

```text
outputs/<run_id>/state/memory.sqlite3
```

优势：

- Base 和 MiM 不会共享脏状态；
- 重跑容易；
- 不需要复杂 run_id 条件；
- 文件可以连同 config 一起归档；
- test 不会污染 train。

`use` 命令可以使用长期 workspace：

```text
workspace/memory.sqlite3
```

### 4.3 Skill 隔离

Skill Bank 跨 train conversation 共享，但事实 Memory 不共享：

```text
事实 Memory：conversation scoped
程序性 Skill：split/run scoped
```

---

## 5. 原始消息 Schema

原始消息必须先保存，再运行 Construction。

```sql
CREATE TABLE conversations (
    conversation_id TEXT PRIMARY KEY,
    split_name      TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE sessions (
    session_id       TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    session_index    INTEGER NOT NULL,
    occurred_at      TEXT,
    content_hash     TEXT NOT NULL,
    FOREIGN KEY (conversation_id)
        REFERENCES conversations(conversation_id),
    UNIQUE (conversation_id, session_index)
);

CREATE TABLE messages (
    message_id       TEXT PRIMARY KEY,
    conversation_id  TEXT NOT NULL,
    session_id       TEXT NOT NULL,
    turn_index       INTEGER NOT NULL,
    role             TEXT NOT NULL,
    speaker          TEXT,
    content          TEXT NOT NULL,
    occurred_at      TEXT,
    content_hash     TEXT NOT NULL,
    FOREIGN KEY (session_id)
        REFERENCES sessions(session_id),
    UNIQUE (session_id, turn_index)
);

CREATE INDEX idx_messages_conversation_session
ON messages(conversation_id, session_id, turn_index);
```

要求：

- `message_id` 必须稳定；
- timestamp 尽量标准化为 ISO 8601；
- 不改写原文；
- Construction 生成的每条 Memory 必须引用至少一个 `message_id`；
- source 不存在时拒绝写入。

---

## 6. Construction Commit

每个 session 产生一个原子 commit。

```sql
CREATE TABLE construction_commits (
    commit_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id    TEXT NOT NULL,
    session_id         TEXT NOT NULL,
    base_commit_id     INTEGER,
    run_id             TEXT NOT NULL,
    status             TEXT NOT NULL,
    runtime_model      TEXT NOT NULL,
    prompt_hash        TEXT NOT NULL,
    skill_version_ids  TEXT NOT NULL,
    plan_json          TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    completed_at       TEXT,
    error_message      TEXT
);

CREATE INDEX idx_commits_conversation
ON construction_commits(conversation_id, commit_id);
```

`status`：

```text
pending
committed
failed
```

一个 commit 保存：

- 基于哪个旧 commit 构建；
- 使用哪个 runtime model；
- 使用哪些 Construction Skills；
- Agent 生成的完整 Construction Plan；
- 成功或失败。

### 6.1 为什么 LLM 调用不放在数据库事务中

LLM 调用可能持续数十秒。如果一直持有 SQLite write lock，会影响可靠性。

正确流程：

```text
1. 读取 base_commit_id
2. 读取当前 Memory snapshot
3. 调用 Construction Agent 生成计划
4. 校验计划
5. BEGIN IMMEDIATE
6. 再检查 latest_commit_id == base_commit_id
7. 应用全部动作
8. 写 committed
9. COMMIT
```

如果第 6 步发现状态变化，放弃该计划并重新构建，不能在过期 Memory 上直接提交。

评测时 conversation 串行，一般不会发生冲突，但保留该检查有助于代码正确。

---

## 7. Memory Unit

### 7.1 设计原则

每条 Memory：

- 只表达一个主要事实、状态或事件；
- 不使用无法解析的代词；
- 包含明确主体；
- 尽量包含绝对时间；
- 可以独立理解；
- 保留原始消息来源；
- 可以有多个版本。

### 7.2 Memory 类型

第一版固定六类：

```text
profile
  相对稳定的身份、背景和属性。

preference
  喜好、习惯和倾向。

state
  会随时间变化的状态，如居住地、工作、关系状态。

event
  发生过的具体事件。

plan
  未来安排、承诺和待办。

relationship
  人物或实体间的关系。
```

不确定类型时使用 `event`，不额外增加 `other` 类型。

### 7.3 Memory Version Schema

```sql
CREATE TABLE memory_versions (
    row_id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id               TEXT NOT NULL UNIQUE,
    memory_id                TEXT NOT NULL,
    version_no               INTEGER NOT NULL,
    conversation_id          TEXT NOT NULL,

    memory_kind              TEXT NOT NULL,
    subject                  TEXT NOT NULL,
    predicate                TEXT,
    object_text              TEXT,
    content                  TEXT NOT NULL,

    world_start              TEXT,
    world_end                TEXT,
    recorded_at              TEXT NOT NULL,

    system_from_commit       INTEGER NOT NULL,
    system_to_commit         INTEGER,
    close_reason             TEXT,

    source_message_ids       TEXT NOT NULL,
    entities_json            TEXT NOT NULL,
    keywords_json            TEXT NOT NULL,
    related_memory_ids       TEXT NOT NULL,

    importance               REAL NOT NULL,
    confidence               REAL NOT NULL,

    content_hash             TEXT NOT NULL,
    embedding_blob           BLOB NOT NULL,
    embedding_dim            INTEGER NOT NULL,
    embedding_model          TEXT NOT NULL,

    parent_version_id        TEXT,
    update_type              TEXT NOT NULL,
    created_by_skill_ids     TEXT NOT NULL,

    UNIQUE (memory_id, version_no)
);

CREATE INDEX idx_memory_conversation_current
ON memory_versions(
    conversation_id,
    system_to_commit
);

CREATE INDEX idx_memory_subject_predicate
ON memory_versions(
    conversation_id,
    subject,
    predicate
);

CREATE INDEX idx_memory_world_time
ON memory_versions(
    conversation_id,
    world_start,
    world_end
);

CREATE INDEX idx_memory_hash
ON memory_versions(
    conversation_id,
    content_hash
);
```

### 7.4 两种时间

必须区分：

```text
world time
  事实在用户世界中何时成立。
  world_start / world_end

system time
  系统从哪个 Construction commit 起认为该版本有效。
  system_from_commit / system_to_commit
```

示例：

```text
session 1:
  Alice lives in Boston.

session 3:
  Alice moved to Seattle in May.
```

可以形成：

```text
mem_residence v1
  content: Alice lived in Boston.
  world_start: unknown
  world_end: 2023-05
  system_from_commit: 1
  system_to_commit: 3

mem_residence v2
  content: Alice has lived in Seattle since May 2023.
  world_start: 2023-05
  world_end: null
  system_from_commit: 3
  system_to_commit: null
```

因此：

- 问“现在”时找到 v2；
- 问“搬家前”时找到 v1；
- Replay commit 2 时只看到当时系统已知的 v1。

### 7.5 生命周期

`close_reason`：

```text
superseded
retracted
merged
```

当前有效版本的 `system_to_commit` 和 `close_reason` 都为空。关闭旧版本时写入终止 commit 和原因。读取某个历史 commit 时以 system interval 推导当时是否有效，不能只看今天的 `close_reason`。

`update_type`：

```text
add
state_change
correction
enrichment
merge
```

区别：

- `state_change`：旧事实过去正确，但现在状态变化；
- `correction`：旧事实本身错误；
- `enrichment`：事实未冲突，只增加来源或细节；
- `merge`：合并重复 Memory。

Content 与 embedding 写入后不可原地修改。允许在后续 commit 中关闭：

- `system_to_commit`；
- `world_end`；
- `close_reason`。

这些关闭操作必须记录在新的 Construction commit 中。

---

## 8. FTS5 索引

```sql
CREATE VIRTUAL TABLE memory_fts USING fts5(
    version_id UNINDEXED,
    conversation_id UNINDEXED,
    content,
    subject,
    predicate,
    object_text,
    keywords,
    tokenize = 'porter unicode61'
);
```

每次插入 Memory version 时，同步插入 `memory_fts`。

不使用 external-content trigger，第一版由 `SQLiteMemoryStore` 在同一事务中显式维护：

```text
INSERT memory_versions
INSERT memory_fts
```

版本不会物理删除，因此 FTS 记录也不删除。查询结果通过 join 回 `memory_versions`，再应用 conversation、commit、status 和时间过滤。

说明：

- LoCoMo 主要为英文，`porter unicode61` 足够；
- 中文未来主要依赖语义检索；
- 若后续做中文关键词检索，再增加分词器，不作为当前 MVP。

---

## 9. Embedding 存储

### 9.1 推荐模型

配置化：

```yaml
embedding:
  model: Qwen/Qwen3-Embedding-0.6B
  dimension: 1024
  device: cpu
  normalize: true
  batch_size: 32
```

如果本机资源不足，可在 smoke test 使用：

```text
sentence-transformers/all-MiniLM-L6-v2
```

正式 Base/MiM 对比必须使用同一 embedding model。

### 9.2 BLOB 格式

```python
blob = vector.astype(np.float32).tobytes()
```

读取：

```python
vector = np.frombuffer(blob, dtype=np.float32)
```

每条记录同时保存：

- `embedding_model`；
- `embedding_dim`。

如果当前配置与数据库中已有向量不一致：

- 默认拒绝启动；
- 由显式 `reindex` 命令重建；
- 不能在一个 run 中混用不同 embedding。

### 9.3 为什么不使用独立向量数据库

LoCoMo 每个 conversation 的 Memory 规模有限。

查询时：

1. SQL 过滤当前 conversation 与 snapshot；
2. 一次取出几百或几千个 float32 vector；
3. NumPy 批量点积；
4. 取 Top-N。

这是真实可运行的向量检索，不是伪检索。达到数万/数十万条 Memory 后，可以把 `semantic_search()` 替换为 LanceDB/Faiss，而不修改 Agent。

---

## 10. Construction Candidate

Construction Agent 不直接把整段 session 变成一个大摘要，而是先生成原子候选：

```json
{
  "candidate_id": "cand_03",
  "memory_kind": "state",
  "subject": "Alice",
  "predicate": "residence",
  "object_text": "Seattle",
  "content": "Alice moved to Seattle in May 2023.",
  "world_start": "2023-05",
  "world_end": null,
  "source_message_ids": ["conv0_s3_m8"],
  "entities": ["Alice", "Seattle"],
  "keywords": ["Alice", "Seattle", "move", "residence"],
  "importance": 0.8,
  "confidence": 0.95
}
```

校验规则：

- `source_message_ids` 非空且存在；
- `content` 不得含不明确代词；
- `content` 不得加入 source 中没有的信息；
- `subject` 必填；
- `world_end >= world_start`；
- importance/confidence 在 `[0, 1]`；
- candidate 数量不超过配置；
- 重复 hash 在进入 Agent 决策前去重。

---

## 11. Construction Agent 完整 Workflow

### 11.1 总流程

```text
原始 session
→ 保存 messages
→ 读取 base Memory snapshot
→ 检索 Construction Skills
→ Stage A：候选抽取
→ 每个 Candidate 检索相关旧 Memory
→ Stage B：写入决策
→ 必要时 SEARCH_MORE 并再次决策
→ Python 校验 Construction Plan
→ SQLite 原子提交
→ 生成 commit_id / snapshot
→ 记录 trace
```

Construction Agent 是一个逻辑 Agent，但内部使用两个明确阶段。

### 11.2 Stage A：Candidate Extraction

输入：

- 当前 session 原文；
- session absolute time；
- Construction Skills；
- Memory schema。

不向该阶段注入整个 Memory DB，避免上下文膨胀。

输出：

```json
{
  "candidates": []
}
```

要求：

- atomic；
- self-contained；
- source-grounded；
- absolute time；
- durable information；
- 允许输出空列表。

### 11.3 Session 分块

LoCoMo session 正常情况下整段处理。超过限制时：

```yaml
construction:
  max_input_tokens: 12000
  window_messages: 40
  overlap_messages: 4
```

长 session：

1. 按 message 顺序切 window；
2. 每个 window 抽 Candidate；
3. 用 `source_message_ids + content_hash` 去重；
4. 合并后统一进入 Stage B；
5. 整个 session 仍只产生一个 commit。

### 11.4 Candidate Existing-Memory Retrieval

每个 Candidate 自动执行四路查找：

1. Exact：
   - 相同 content hash；
2. Key：
   - 相同/近似 `subject + predicate`；
3. Semantic：
   - Candidate content 向量 Top-5；
4. Entity-Time：
   - 实体相交且时间重叠/相邻。

合并后最多给 Stage B 8 条 Existing Memory。

该检索由 Python 自动完成，不消耗 Agent action step。

### 11.5 Stage B：Construction Decision

Agent 同时看到：

- Candidate；
- 相关 Existing Memory；
- Skill；
- 每种 action 的严格定义。

如果现有候选不足以判断重复、冲突或状态变化，Agent 可以先返回：

```json
{
  "action": "SEARCH_MORE",
  "candidate_id": "cand_03",
  "query": "Alice previous residence and moves",
  "include_history": true,
  "reason": "The first candidates do not establish whether this is a new state."
}
```

Python 执行额外检索并把结果返回给同一个 Construction Agent。默认每个 session 最多两次 `SEARCH_MORE`，Base 与 MiM 预算相同。达到预算后必须输出最终计划。

最终输出一个批量 Construction Plan：

```json
{
  "base_commit_id": 12,
  "decisions": [
    {
      "candidate_id": "cand_03",
      "action": "UPDATE",
      "target_memory_id": "mem_residence_alice",
      "update_type": "state_change",
      "reason": "The residence changed from Boston to Seattle.",
      "merged_content": "Alice has lived in Seattle since May 2023.",
      "world_start": "2023-05",
      "world_end": null,
      "source_message_ids": ["conv0_s3_m8"]
    }
  ]
}
```

### 11.6 Actions

#### SEARCH_MORE

适用：

- 自动 shortlist 证据不足；
- 需要检查历史版本；
- subject/predicate 抽取不完全一致；
- 可能存在跨 session 重复或冲突。

执行：

- 只读；
- 不产生 commit；
- query 和结果写 trace；
- 返回 Stage B 继续决策。

#### ADD

适用：

- 新事实；
- 新事件；
- 没有语义等价的旧 Memory。

执行：

- 新 `memory_id`；
- `version_no=1`；
- active；
- 写 embedding 和 FTS。

#### UPDATE

适用：

- state change；
- correction；
- enrichment。

执行：

- 关闭旧 version 的 system interval；
- 必要时关闭旧 world interval；
- 同一 `memory_id` 创建新 version；
- 新 version 指向 parent。

#### MERGE

适用：

- 多条旧 Memory 实质重复；
- 新 Candidate 能把它们合成更完整的同一事实。

执行：

- 选一个 canonical `memory_id`；
- 关闭被合并版本；
- 创建 canonical 新 version；
- union source IDs；
- `update_type=merge`。

#### SKIP

适用：

- 完全重复；
- 短期寒暄；
- 无来源；
- 低置信度且不可验证；
- 对长期问答没有意义。

SKIP 只写 trace，不写 Memory version。

### 11.7 硬校验

在执行 SQL 前，Python 必须拒绝：

- target memory 不存在；
- target 属于另一 conversation；
- source message 不存在；
- UPDATE 没有 parent；
- ADD 错用已有 memory_id；
- world time 非法；
- Candidate 重复决策；
- action 不在白名单；
- plan 的 base commit 已过期；
- merged content 没有任何 source 支持。

### 11.8 提交失败

任何一个 action 失败：

- 整个 session rollback；
- commit 标记 failed；
- 原 Memory 不变；
- 记录具体 validation error；
- 允许对同一 session 重试。

不能只写入半个 Construction Plan。

---

## 12. Construction Store API

```python
class MemoryStore(Protocol):
    def save_session(self, session: Session) -> None:
        ...

    def latest_commit_id(self, conversation_id: str) -> int | None:
        ...

    def load_snapshot(
        self,
        conversation_id: str,
        as_of_commit: int | None = None,
        include_history: bool = False,
    ) -> list[MemoryRecord]:
        ...

    def find_related_for_construction(
        self,
        conversation_id: str,
        candidate: MemoryCandidate,
        as_of_commit: int,
        limit: int = 8,
    ) -> list[MemoryHit]:
        ...

    def apply_construction_plan(
        self,
        conversation_id: str,
        session_id: str,
        base_commit_id: int | None,
        plan: ConstructionPlan,
        run_context: RunContext,
    ) -> ConstructionCommit:
        ...
```

Agent 不持有 sqlite connection。

---

## 13. Access & Answer Agent 完整 Workflow

### 13.1 总流程

```text
question
→ 检索 Access Skills
→ 初始化 evidence workspace
→ Access & Answer Agent 选择 action
   ├─ search_memory
   ├─ inspect_memory
   └─ answer
→ observation 回到同一个 Agent 上下文
→ 继续检索或回答
→ evidence ID 校验
→ 输出最终答案
```

Access 和 Answer 是同一个 Agent。没有单独 Answer Model。

### 13.2 Evidence Workspace

每道 QA 建立内存中的 workspace：

```python
class EvidenceWorkspace:
    question: str
    snapshot_commit_id: int
    search_history: list[SearchCall]
    visible_hits: dict[str, MemoryHit]
    inspected_sources: dict[str, SourceBundle]
    used_skill_ids: list[str]
    remaining_steps: int
```

Agent 每轮看到：

- 原问题；
- 已使用 Skill；
- 搜索历史摘要；
- 当前可见 Memory；
- 剩余 step；
- action schema。

不会每轮重复注入完整 trace。

---

## 14. Access Actions

### 14.1 search_memory

```json
{
  "action": "search_memory",
  "arguments": {
    "query": "Alice residence before moving to Seattle",
    "strategy": "hybrid",
    "entities": ["Alice"],
    "memory_kinds": ["state"],
    "time_mode": "before",
    "target_time": "2023-05",
    "include_history": true,
    "top_k": 8
  },
  "reason": "The question asks for the previous state."
}
```

`strategy`：

```text
hybrid
semantic
keyword
temporal
```

默认建议 `hybrid`。

### 14.2 inspect_memory

```json
{
  "action": "inspect_memory",
  "arguments": {
    "memory_id": "mem_residence_alice",
    "include_versions": true,
    "include_sources": true
  },
  "reason": "The search returned multiple residence versions."
}
```

返回：

- 该 logical Memory 的版本链；
- world/system time；
- source message excerpts；
- merge/correction 信息。

`inspect_memory` 解决：

- 时间歧义；
- 判断旧版本为何失效；
- 验证 Memory 是否忠于 source。

Source excerpt 只允许访问该 Memory 已声明的 source IDs，不允许任意读取整段 raw conversation。

### 14.3 answer

```json
{
  "action": "answer",
  "arguments": {
    "answer": "Boston",
    "evidence_version_ids": ["mem_residence_alice_v1"],
    "confidence": 0.91
  },
  "reason": "Version 1 was valid before the move to Seattle."
}
```

校验：

- evidence 必须已经进入 workspace；
- evidence 必须属于当前 conversation；
- evidence 必须在指定 snapshot 可见；
- confidence 在 `[0,1]`；
- 无证据时只允许输出 dataset 规定的不可回答形式。

---

## 15. Hybrid Retrieval

### 15.1 为什么默认 Hybrid

单一路径的典型问题：

- Semantic：容易召回语义相近但时间错误的版本；
- Keyword：容易漏掉同义表达；
- Structured：依赖 metadata 抽取正确；
- Temporal：只有时间，没有语义不足以回答。

Hybrid 同时执行三路召回，再稳定融合。

### 15.2 Query Filters

所有路径先固定：

```text
conversation_id = current conversation
as_of_commit = current snapshot
```

然后应用：

- memory kinds；
- entities；
- time mode；
- include history。

### 15.3 Semantic Path

1. SQL 取出符合 namespace/snapshot 的 version IDs 和 embeddings；
2. embedding query；
3. NumPy dot product；
4. Top `semantic_candidate_k`。

配置：

```yaml
retrieval:
  semantic_candidate_k: 30
```

向量已归一化时：

```python
scores = matrix @ query_vector
```

### 15.4 Keyword Path

FTS5：

```sql
SELECT
    f.version_id,
    bm25(memory_fts) AS bm25_score
FROM memory_fts AS f
JOIN memory_versions AS m
    ON m.version_id = f.version_id
WHERE
    memory_fts MATCH ?
    AND m.conversation_id = ?
    AND m.system_from_commit <= ?
    AND (
        ? = 1
        OR m.system_to_commit IS NULL
        OR m.system_to_commit > ?
    )
LIMIT ?;
```

其中布尔参数表示 `include_history`。显式时间、memory kind、entity 和 retracted 过滤必须在取最终 Top-N 前应用，不能先截断 FTS 结果再过滤。

配置：

```yaml
retrieval:
  keyword_candidate_k: 30
```

### 15.5 Structured Path

结构化候选来自：

- `subject` 精确/规范化匹配；
- `predicate` 匹配；
- entity intersection；
- memory kind；
- world time。

配置：

```yaml
retrieval:
  structured_candidate_k: 30
```

### 15.6 Temporal Filtering

#### 无时间条件

默认：

- current active versions；
- `include_history=false`。

#### current

优先：

- `world_end IS NULL`；
- 当前 active；
- 最新 `world_start`。

#### point

目标时间 `t`：

```text
world_start <= t
and (world_end is null or t < world_end)
```

#### before

优先：

```text
world_start < target_time
```

并按最接近 target_time 的过去状态排序。

#### after

优先：

```text
world_start >= target_time
```

#### range

时间区间有交集：

```text
memory.world_start <= query.end
and
(memory.world_end is null or memory.world_end >= query.start)
```

未知 world time：

- 显式时间问题中降权；
- 不直接删除，避免时间抽取失败导致完全漏召回。

---

## 16. 检索融合

### 16.1 Weighted Reciprocal Rank Fusion

不直接相加 cosine、BM25 和 SQL 分数，因为三个分数不在同一尺度。

使用 Weighted RRF：

```text
rrf(m) =
    w_sem / (k + rank_sem)
  + w_key / (k + rank_key)
  + w_struct / (k + rank_struct)
```

默认：

```yaml
retrieval:
  rrf_k: 60
  semantic_weight: 0.45
  keyword_weight: 0.30
  structured_weight: 0.25
```

某路径未命中时该项为 0。

### 16.2 Match Multiplier

RRF 后应用小幅乘法修正：

```text
entity exact match:  × 1.10
explicit time valid: × 1.20
current question and active version: × 1.05
explicit temporal mismatch: × 0.50
```

所有权重放在配置中，并在 Base/MiM 固定一致。

### 16.3 Logical Memory 去重

普通当前问题：

- 同一 `memory_id` 默认只保留最高分版本。

显式历史问题或 `include_history=true`：

- 允许同一 `memory_id` 返回多个版本；
- 每个版本保留独立 `version_id`。

### 16.4 最终返回

最终 Top-k 每条包含：

```json
{
  "rank": 1,
  "version_id": "mem_residence_alice_v1",
  "memory_id": "mem_residence_alice",
  "content": "Alice lived in Boston before May 2023.",
  "memory_kind": "state",
  "world_start": null,
  "world_end": "2023-05",
  "entities": ["Alice", "Boston"],
  "source_message_ids": ["conv0_s1_m2"],
  "score": 0.0184,
  "matched_paths": ["semantic", "keyword", "structured"]
}
```

不把 embedding 返回给 Agent。

---

## 17. Query 与时间解析

Access Agent 在每次 `search_memory` action 中显式输出：

- query；
- entities；
- memory kinds；
- time mode；
- target time；
- include history。

时间解析分两层：

1. Agent 根据自然语言问题输出结构化时间；
2. Python 使用 ISO/date parser 校验和标准化。

校验失败：

- observation 明确返回错误；
- Agent 可以修正一次；
- 仍失败时回退到 `time_mode=none` 并记录 fallback；
- 不在代码中默默猜一个年份。

---

## 18. Access Loop 与预算

默认：

```yaml
access:
  max_steps: 6
  max_search_calls: 4
  max_inspect_calls: 2
  result_top_k: 8
  max_visible_memories: 16
  max_source_messages: 8
```

一次 step 是：

```text
Agent 输出一个 action
→ Python 执行
→ observation 回到 Agent
```

达到预算仍未 answer：

1. 禁止继续调用工具；
2. 用当前 evidence 要求同一个 Agent 输出最终 answer；
3. 标记 `forced_final_answer=true`。

Base 与 MiM 使用完全相同预算。

---

## 19. Access Context 管理

避免把每轮所有结果无限追加：

### 19.1 可见证据

- 同一 version ID 只保存一次；
- 最多 16 条；
- 新高分证据可以替换未使用的低分证据；
- 被 inspect 或被 Agent 标记重要的证据不可被替换。

### 19.2 Observation

每次 search 返回：

- query；
- filters；
- Top-k；
- score/path；
- 是否有更多候选。

### 19.3 Search History

给 Agent 的历史只保留摘要：

```text
Step 1: hybrid "Alice residence" → 8 hits
Step 2: temporal before 2023-05 → 3 hits
```

完整结果写 trace，不重复塞入 prompt。

---

## 20. Answer Grounding

Answer 必须附 `evidence_version_ids`。

Python 执行三项检查：

1. evidence 是否已在 workspace；
2. evidence 是否在 snapshot 有效；
3. evidence 是否属于当前 conversation。

如果失败：

- 返回 observation；
- 允许 Agent 修正一次；
- 再失败标记 `invalid_evidence_protocol`。

### 20.1 无答案

如果 Memory 中无支持证据：

- 不允许编造；
- 输出 LoCoMo category 5 所需的不可回答形式；
- evidence 列表允许为空；
- trace 记录 Agent 已执行的搜索。

### 20.2 多跳

多跳问题：

- Agent 可进行多次 search；
- 最终 evidence 可以包含多个 Memory version；
- Answer prompt 要求说明这些证据如何组合，但对外答案保持简洁。

---

## 21. Snapshot 读取

### 21.1 当前 Snapshot

当前版本：

```sql
system_to_commit IS NULL
```

### 21.2 指定 Commit 的当前有效视图

在 commit `c` 时可见：

```text
system_from_commit <= c
and
(system_to_commit is null or system_to_commit > c)
```

这用于：

- Access 正常运行；
- Failure Attribution；
- Access Replay；
- 检查 Construction 前后差异。

### 21.3 指定 Commit 的已知历史

当 `include_history=true` 时，目标不是只返回 commit `c` 时仍 active 的版本，而是返回系统在 `c` 之前已经知道的版本：

```text
system_from_commit <= c
```

然后：

- `close_reason=superseded` 的旧状态可用于历史时间问题；
- `close_reason=merged` 的版本通常只用于 inspect 和溯源；
- `close_reason=retracted` 默认不作为回答证据，除非问题明确询问错误记录本身；
- `system_from_commit > c` 的未来版本绝不能出现。

这一区分保证当前 commit 能回答过去状态，同时避免 Replay 看到未来才写入的 Memory。

### 21.4 历史 World State

注意：

```text
as_of_commit
```

回答系统当时知道什么；

```text
world_start/world_end
```

回答事实在现实时间何时成立。

两者不能混用。

---

## 22. Construction 与 Access 的 Skill 注入点

### 22.1 Construction Skills

注入：

- Stage A Candidate Extraction；
- Stage B Construction Decision。

同一组 Skill 在两个阶段可使用，但 prompt 解释不同：

```text
Extraction:
  哪类信息容易漏写，应该抽出什么。

Decision:
  遇到旧 Memory 时应 ADD/UPDATE/MERGE/SKIP。
```

### 22.2 Access Skills

注入 Access loop system context：

```text
何时用 temporal；
何时 include_history；
如何拆解多跳查询；
何时 inspect source；
何时停止回答。
```

### 22.3 Skill 不改变

Skill 不得改变：

- Store schema；
- Retrieval 算法；
- RRF 权重；
- action 白名单；
- max steps；
- top-k。

因此 Base/MiM 的唯一差异仍然是自然语言策略。

---

## 23. Trace

### 23.1 Construction Trace

每个 session 保存：

```json
{
  "conversation_id": "conv0",
  "session_id": "conv0_s3",
  "base_commit_id": 2,
  "commit_id": 3,
  "skill_ids": [],
  "candidates": [],
  "existing_memory_hits": {},
  "construction_plan": {},
  "validation_errors": [],
  "token_usage": {},
  "latency_ms": 0
}
```

### 23.2 Access Trace

每个 QA 保存：

```json
{
  "conversation_id": "conv0",
  "qa_id": "qa14",
  "snapshot_commit_id": 8,
  "question": "...",
  "skill_ids": [],
  "actions": [],
  "visible_evidence_ids": [],
  "final_evidence_ids": [],
  "answer": "...",
  "token_usage": {},
  "latency_ms": 0
}
```

Failure Agent 依赖这些字段进行归因。

---

## 24. Replay 如何使用真实存储

### 24.1 Construction Replay

1. 新建临时 SQLite；
2. 导入失败 conversation 的原始 messages；
3. 逐 session 重放到失败点；
4. 仅在目标阶段强制注入 Candidate Construction Skill；
5. 生成新的 commit 链；
6. 重新运行失败 QA；
7. 比较 Memory、retrieval、answer。

不能直接手工改失败时的 final Memory。

### 24.2 Access Replay

1. 使用原 run SQLite；
2. 固定 `snapshot_commit_id`；
3. 强制或自然注入 Candidate Access Skill；
4. 重新执行 Access loop；
5. 不允许写 Memory。

### 24.3 Replay 可复现字段

- database hash；
- snapshot commit；
- runtime model；
- prompt hash；
- embedding model；
- retrieval config；
- Skill version；
- max steps；
- seed。

---

## 25. Store 与 Retrieval 文件树

在 `TASK1_RESULT.md` 的项目树基础上，运行侧建议细化为：

```text
src/mim/
├─ schemas.py
├─ agents/
│  ├─ construction.py
│  └─ access.py
├─ storage/
│  ├─ __init__.py
│  ├─ schema.sql
│  ├─ sqlite_store.py
│  └─ vector_codec.py
├─ retrieval/
│  ├─ __init__.py
│  ├─ embedder.py
│  ├─ semantic.py
│  ├─ keyword.py
│  ├─ temporal.py
│  └─ hybrid.py
├─ workflows/
│  └─ use.py
└─ tracing.py
```

这比单个 `memory.py + retrieval.py` 多几个文件，但属于必要拆分：

- SQL/schema 与 Agent 分开；
- 三路检索可单测；
- Hybrid 只负责融合；
- 后续替换 semantic backend 不影响其他模块。

仍然不需要：

- repository/service/controller 多层套娃；
- ORM；
- dependency injection framework；
-数据库服务。

---

## 26. 关键 Python 接口

### 26.1 Construction

```python
class ConstructionAgent:
    def extract_candidates(
        self,
        session: Session,
        skills: list[SkillRecord],
    ) -> list[MemoryCandidate]:
        ...

    def build_plan(
        self,
        base_commit_id: int | None,
        candidates: list[MemoryCandidate],
        related_memories: dict[str, list[MemoryHit]],
        skills: list[SkillRecord],
    ) -> ConstructionPlan:
        ...
```

### 26.2 Access & Answer

```python
class AccessAnswerAgent:
    def answer(
        self,
        question: Question,
        conversation_id: str,
        snapshot_commit_id: int,
        retriever: HybridRetriever,
        store: MemoryStore,
        skills: list[SkillRecord],
        budget: AccessBudget,
    ) -> AccessResult:
        ...
```

### 26.3 Hybrid Retriever

```python
class HybridRetriever:
    def search(
        self,
        *,
        conversation_id: str,
        snapshot_commit_id: int,
        query: str,
        strategy: SearchStrategy,
        filters: SearchFilters,
        top_k: int,
    ) -> list[MemoryHit]:
        ...
```

### 26.4 Inspect

```python
class MemoryStore:
    def inspect_memory(
        self,
        *,
        conversation_id: str,
        memory_id: str,
        snapshot_commit_id: int,
        include_versions: bool,
        include_sources: bool,
    ) -> MemoryInspection:
        ...
```

---

## 27. 推荐配置

```yaml
storage:
  backend: sqlite
  path: outputs/${RUN_ID}/state/memory.sqlite3
  busy_timeout_ms: 5000
  journal_mode: WAL
  foreign_keys: true

embedding:
  model: Qwen/Qwen3-Embedding-0.6B
  dimension: 1024
  device: cpu
  normalize: true
  batch_size: 32

construction:
  max_input_tokens: 12000
  window_messages: 40
  overlap_messages: 4
  max_candidates_per_session: 30
  related_memory_limit: 8
  max_search_more_calls: 2
  exact_duplicate_threshold: 1.0
  semantic_duplicate_candidate_threshold: 0.88

retrieval:
  semantic_candidate_k: 30
  keyword_candidate_k: 30
  structured_candidate_k: 30
  result_top_k: 8
  rrf_k: 60
  semantic_weight: 0.45
  keyword_weight: 0.30
  structured_weight: 0.25
  entity_match_multiplier: 1.10
  time_valid_multiplier: 1.20
  current_active_multiplier: 1.05
  temporal_mismatch_multiplier: 0.50

access:
  max_steps: 6
  max_search_calls: 4
  max_inspect_calls: 2
  max_visible_memories: 16
  max_source_messages: 8
```

阈值需要在 validation 上固定。禁止根据 test 调整。

---

## 28. 实现顺序

### Phase A：Store

1. `schema.sql`；
2. message/session 保存；
3. Memory version insert/update；
4. commit transaction；
5. snapshot query；
6. FTS5；
7. vector BLOB codec。

验收：

- 两个 session 的 state change 形成 v1/v2；
- commit 1/commit 2 能返回不同 snapshot；
- rollback 不产生半写入。

### Phase B：Construction

1. Candidate schema；
2. Extraction prompt；
3. existing-memory retrieval；
4. Construction Plan；
5. Python validation；
6. apply plan。

验收：

- duplicate → SKIP；
- 新事实 → ADD；
- 状态变化 → UPDATE/state_change；
- 错误纠正 → UPDATE/correction；
- 多来源重复 → MERGE；
- 所有结果有 source。

### Phase C：Retrieval

1. semantic；
2. FTS5 keyword；
3. structured/time；
4. RRF；
5. logical Memory 去重；
6. inspect。

验收：

- 同义问题 semantic 命中；
- 专名 keyword 命中；
- 当前/历史状态 temporal 命中正确版本；
- 多路结果排序稳定。

### Phase D：Access & Answer

1. EvidenceWorkspace；
2. search action；
3. inspect action；
4. answer action；
5. evidence validation；
6. budget fallback。

验收：

- 单跳；
- 多跳；
- 当前状态；
- 历史状态；
- 不可回答；
- 非法 evidence。

### Phase E：MiM/Replay

1. Skill 注入；
2. Construction replay；
3. Access replay；
4. Failure trace 对接；
5. Base/MiM 公平性检查。

---

## 29. 必须测试的真实案例

### 29.1 Duplicate

多个 session 重复说同一事实：

```text
预期：同一 Memory，不无限新增。
```

### 29.2 State Change

```text
Alice lived in Boston.
Alice moved to Seattle.
```

预期：

- 版本链；
- 当前问题返回 Seattle；
- 历史问题返回 Boston。

### 29.3 Correction

```text
Earlier statement was wrong; the meeting is on Friday, not Thursday.
```

预期：

- Thursday 版本 retracted；
- Friday 成为 active；
- source 可追溯。

### 29.4 Enrichment

```text
Alice is attending a conference.
The conference is ACL in Vienna.
```

预期：

- 可以 enrichment/merge；
- 不丢失第一条 source。

### 29.5 Multi-hop

```text
Alice joined Company X.
Company X is based in Berlin.
Question: Which city is Alice's company based in?
```

预期：

- 两次 search 或一次 hybrid 找到两条；
- 最终 evidence 包含两条。

### 29.6 No Answer

Memory 中不存在答案：

```text
预期：Not mentioned；不能凭模型常识回答。
```

### 29.7 Cross-conversation Isolation

两个 conversation 都有 Alice：

```text
预期：绝不跨 namespace 召回。
```

### 29.8 Snapshot Replay

commit 2 后才知道新状态：

```text
预期：as_of_commit=1 看不到新状态。
```

---

## 30. 何时才需要升级后端

当前 SQLite + NumPy 足以支持：

- LoCoMo；
- 当前 Single-Agent Demo；
- 几千至数万 Memory 的单机实验；
- 完整版本和 Replay。

满足以下情况才考虑 LanceDB/Faiss/PostgreSQL：

- 单 namespace 超过数万到十万向量，NumPy 延迟不可接受；
- 需要多进程并发写；
- 需要远程服务；
- 需要 ANN 索引；
- 需要多个外部基座共享存储。

升级时只替换：

```text
semantic.py
sqlite_store.py 的向量部分
```

Construction/Access Agent、Skill、Workflow 和 trace 接口保持不变。

---

## 31. 最终判断

这套运行侧不再是玩具，原因是：

1. 原始消息、Memory、版本和 commit 都有真实持久化；
2. session 更新具备原子事务；
3. 能区分现实时间与系统认知时间；
4. 能处理新增、状态变化、纠正、补充、合并和跳过；
5. 检索包含 semantic、FTS5 keyword、structured 和 temporal；
6. 多路分数使用可复现的 RRF 融合；
7. Access & Answer 在同一 loop 中迭代检索、检查证据并回答；
8. evidence 经过程序校验；
9. 任意 snapshot 可以按 commit 重建；
10. Construction 与 Access Replay 都有真实状态基础；
11. Base 与 MiM 仍然只相差 Skill；
12. 整体仍是单机、单 SQLite、无服务依赖的最小架构。

实现时优先保证：

```text
正确版本
→ 正确检索
→ 正确证据
→ 可回放轨迹
```

不要先增加更多 Agent、图结构或基础设施。

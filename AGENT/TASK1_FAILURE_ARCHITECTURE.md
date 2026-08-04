# MiM Failure Architecture

## 基于消息—Memory 版本血缘的错误定位、溯源与学习路由

> 文档定位：独立的维护侧 MiM Failure 子系统实现级工程设计，不属于 Runtime Workflow，也不是 Runtime 文档的追加章节。  
> 输入边界：只读消费运行侧导出的标准化 Message、Construction、Memory Version、snapshot 和 Access Trace 契约；Failure 核心不依赖 Runtime 内部类。  
> 核心原则：纯算法负责建立事实血缘和定位断点，强模型负责语义判断，Replay 负责验证因果关系。

---

## 1. 目标

Failure 子系统接收一个训练阶段的错误 QA，回答：

1. 标准答案由哪些原始 Message 支持；
2. 这些 Message 是否被 Construction 处理；
3. 是否形成 Candidate；
4. Candidate 被 ADD、UPDATE、MERGE 还是 SKIP；
5. 产生了哪些 Memory Version；
6. 后续版本修改影响了哪些历史来源；
7. 正确 Memory 在问题对应 snapshot 是否存在；
8. 正确 Memory 是否被检索，并真实进入最终 Answer Prompt；
9. 第一个错误断点在哪里；
10. 应把失败路由给 Construction Skill-Maker、Access Skill-Maker，还是不学习。

最终形成：

```text
Message
→ Candidate
→ Construction Decision
→ Memory Version
→ Memory Version Lineage
→ Retrieval Hit
→ Answer-visible Memory
→ Selected Evidence
→ Answer
```

---

## 2. 总体架构

```text
Failed QA
   │
   ▼
FailureCaseBuilder
   │ 冻结问题、答案、snapshot、trace 和配置
   ▼
GoldEvidenceBuilder
   │ 标准答案 + LoCoMo source → 原子证据单元
   ▼
ProvenanceService（纯算法，只读）
   │ Message → Candidate → Decision → Memory → Access
   ▼
CoverageMatrixBuilder（纯算法 + 语义判断结果）
   │ 找到每个证据单元经过的阶段
   ▼
FailureAgent（强模型，只读）
   │ 判断 Memory/证据是否在语义上充分
   ▼
FirstBreakLocator（纯算法）
   │ 找到证据链第一个断点并确定候选 subtype
   ▼
CounterfactualVerifier
   │ Oracle Construction / Oracle Access
   ▼
FailureReport
   │ primary + contributors + provenance + confidence
   ▼
LearningRouter
   ├─ Construction Skill-Maker
   ├─ Access Skill-Maker
   └─ Record Only
```

Failure Agent 不是一个拥有数据库写权限的自治服务。它是强模型语义判断器，由 `FailureWorkflow` 编排。

---

### 2.1 与 Runtime 的独立边界

Failure 是离线维护 Workflow：

```text
Runtime 完成一次 train QA
→ 导出 FailureInputBundle
→ Runtime 可结束或释放模型
→ FailureWorkflow 独立启动
→ 只读分析
→ 输出 FailureReport
```

Failure 子系统不得：

- import `ConstructionAgent` 或 `AccessAnswerAgent` 的内部实现；
- 持有 Runtime 的可写 Store；
- 参与正常 `use/evaluate` 请求；
- 改变 Runtime answer；
- 要求 Runtime 为某一种数据库实现暴露 SQL。

二者只通过数据契约连接：

```python
class FailureInputProvider(Protocol):
    def load_case(self, failure_id: str) -> FailureInputBundle:
        ...
```

`FailureInputBundle` 至少包含：

```text
QA 与标准答案
Gold source message IDs
原始 Message records
Construction inputs/candidates/decisions
Memory versions/parents/source edges
snapshot_commit_id
Access actions/hits/final answer-visible context/final evidence
模型、Prompt、Skill 和配置版本
```

其中 `final answer-visible context` 是强制字段：它必须精确记录弱模型生成最终答案时，被序列化进 Answer Prompt 的 Memory Version 集合、顺序和文本。不能用“曾经被 Retriever 返回过”、`kept_in_workspace=true` 或 `final evidence` 反推，因为 Memory 可能在后续步骤被淘汰，final evidence 也可能只是 Agent 的选择结果，而不是它真正看见的全部信息。

运行侧 SQLite 只是一个 Provider 实现：

```text
SQLiteFailureInputProvider
```

测试或外部基座可以使用：

```text
JsonFailureInputProvider
```

Failure 核心只处理 Pydantic 数据对象，不直接依赖 Runtime schema.sql。

Failure 自身产物单独保存：

```text
outputs/<run_id>/failure/
├─ cases/
├─ bundles/
├─ graphs/
├─ reports/
├─ counterfactuals/
└─ cache/
```

如需 Failure 状态数据库，使用独立：

```text
outputs/<run_id>/failure/failure.sqlite3
```

不向 `memory.sqlite3` 写任何 Failure 状态。

---

### 2.2 Failure Agent 采用受控状态机

Failure 归因影响后续 Skill 学习，不能让强模型自由决定跳过哪些检查。工作流固定为：

```text
S0 LOAD_CASE
→ S1 CHECK_RUNTIME_CONTEXT
→ S2 VERIFY_RAW_EVIDENCE
   ├─ Raw Invalid → S8 FINALIZE_REPORT
   └─ Raw Valid
       ├─ Runtime Context 充分 → S8 MODEL_FAILURE
       └─ Runtime Context 不充分 → S3 TRACE_SOURCE_TO_MEMORY
→ S4 JUDGE_SNAPSHOT_MEMORY
   ├─ Snapshot Memory 充分 → S5 TRACE_ACCESS
   └─ Snapshot Memory 不充分 → S6 TRACE_MEMORY_EVOLUTION
→ S7 VERIFY_CAUSALITY
→ S8 FINALIZE_REPORT
```

状态转移由 Python Workflow 控制，不由 LLM 自由选择。

因果链仍然是：

```text
Raw → Memory → Access → Answer
```

但诊断执行顺序从输出端倒查：

```text
Answer 时实际可见的 Memory
→ Snapshot Store
→ Raw Source
→ Construction Version History
```

这样可以最快排除“弱模型已经拿到充分信息但仍然答错”的情况，避免错误更新 MiM Skill。

#### S0 LOAD_CASE

冻结：

- question；
- prediction；
- reference answer；
- Gold source Message IDs；
- `snapshot_commit_id`；
- `access_run_id`；
- config/model/prompt/Skill 版本；
- Runtime database hash。

#### S1 CHECK_RUNTIME_CONTEXT

把 Runtime 弱模型执行最终 `answer` 动作时真正看到的全部 Memory 原样提供给 Failure 强模型。这是 Failure 的第一个实质诊断步骤。

S1 内部使用两个相互隔离的强模型调用：

```text
S1-A CONTEXT_SUFFICIENCY
输入：question + reference answer + 最终可见 Memory
输出：FULL/PARTIAL/CONTRADICTORY/ABSENT、supporting IDs、missing claims

S1-B BLIND_REANSWER
输入：question + 最终可见 Memory + Runtime answer contract
输出：maintenance answer
禁止输入：reference answer、runtime prediction、Raw Conversation
```

两次调用中的 Memory 必须完全相同，均包含：

- answer 时刻仍在 EvidenceWorkspace、且确实被序列化进 Prompt 的全部 Memory；
- 每条 Memory 的 version ID、content、world time 和在 Answer Prompt 中的顺序；
- Answer Prompt 的可验证 hash；
- 不能包含 Raw Conversation；
- 不能包含 Store 中未展示给 Runtime 的 Memory。

`maintenance_answer_correct` 不由强模型自报，而由与 Runtime evaluation 相同的确定性 metric/标准 Judge 在 S1-B 返回后计算。拆成两个调用是必要的：如果让强模型同时看到 reference 并重答，它可能直接复述标准答案，无法证明同一份 Memory 足以让模型答对。

输出：

```text
FULL
PARTIAL
CONTRADICTORY
ABSENT
```

同时输出：

```json
{
  "runtime_context_sufficiency": "FULL",
  "supporting_visible_version_ids": ["mem_state_v3"],
  "missing_claims": [],
  "maintenance_answer": "Seattle",
  "maintenance_answer_correct": true,
  "maintenance_answer_metric": "exact_match_normalized"
}
```

如果：

```text
runtime_context_sufficiency = FULL
AND
maintenance_answer_correct = true
```

则产生高置信度 `model_failure` 假设。仍需执行 S2 验证 reference 本身有效，验证通过后直接进入 S8，不再检查 Construction/Access。

边界处理：

```text
FULL + maintenance_answer_correct
→ model_failure 候选

FULL + maintenance_answer_wrong
→ 强模型判定与重答相互矛盾，review_required=true，不学习 Skill

PARTIAL / ABSENT
→ 继续检查 Snapshot Store 和上游链路

CONTRADICTORY
→ 继续检查 Snapshot Store/Construction，确认冲突来自存储还是 Access 上下文拼接
```

这里的“模型不行”包含弱模型在充分上下文下的推理、遵循指令、证据选择和答案生成失败。只要正确 Memory 已经真实出现在最终 Answer Prompt 中，就不能再把“没选中它”归因成 Access Failure。

#### S2 VERIFY_RAW_EVIDENCE

单独加载 Gold Message 和必要上下文，判断 Raw Conversation 是否真的支持 reference。

注意：S1 和 S2 必须使用不同输入。不能在 S1 中提供 Raw Message，否则强模型可能绕过 Runtime 实际可见 Memory 得出答案。

输出：

```text
SUPPORTED
PARTIAL
CONTRADICTORY
INVALID
```

`INVALID` 直接进入 S8，不学习 Skill。

#### S3 TRACE_SOURCE_TO_MEMORY

调用一个高度集成的纯算法函数：

```python
trace_answer_sources(...)
```

它一次返回：

- Gold Message 是否被 Construction 处理；
- 是否形成 Candidate；
- Candidate 的 Decision；
- 首次生成的 Memory Version；
- 所有后续版本修改；
- Snapshot 中可用版本；
- Access 是否召回、写入最终 Answer Prompt 和选择这些版本。

#### S4 JUDGE_SNAPSHOT_MEMORY

强模型批量判断：

> 问题对应 snapshot 中的相关 Memory，是否在语义和时间上足以回答问题？

如果充分，说明 Construction 主链基本成功，转入 Access 诊断。此处检查的是 Store 中问题 snapshot 可用的相关 Memory，而不是 Runtime 最终看到的上下文。

如果不充分，进入版本演化诊断。

#### S5 TRACE_ACCESS

此时已经知道：

```text
Snapshot Store 有充分 Memory
但 Runtime answer 时的可见 Memory 不充分
```

因此检查正确 Memory 在 Access 路径中的位置：

```text
candidate pool
→ returned hits
→ final answer-visible context
```

#### S6 TRACE_MEMORY_EVOLUTION

从 Gold Message 第一次进入 Memory 开始，沿 Parent/Child 版本图检查：

```text
首次生成
→ enrichment/state_change/correction/merge
→ snapshot 时的版本
```

找到第一个从“语义充分”变为“不充分”的版本变更。

#### S7 VERIFY_CAUSALITY

- Construction 候选：Oracle Construction；
- Access 候选：Oracle Access；
- 明确工程 Bug：不运行 Skill Counterfactual；
- Mixed：先验证 Primary，再记录 contributor。

#### S8 FINALIZE_REPORT

输出：

- Primary label/subtype；
- first broken edge；
-相关 Message/Candidate/Decision/Version/Access IDs；
- 反事实结果；
- confidence；
- Skill / Engineering / Record-only route。

---

### 2.3 高度集成的纯算法溯源函数

建议把主要硬编码逻辑封装为：

```python
class EvidenceTraceService:
    def trace_answer_sources(
        self,
        *,
        failure_case: FailureCase,
        gold_evidence_units: list[GoldEvidenceUnit],
    ) -> AnswerSourceTrace:
        ...
```

输入：

```text
conversation_id
qa_id
Gold source Message IDs
snapshot_commit_id
access_run_id
```

输出：

```python
class AnswerSourceTrace(BaseModel):
    source_traces: list[SourceTrace]
    memory_version_graph: MemoryVersionGraph
    snapshot_memory_versions: list[MemoryVersionRef]
    access_paths: list[AccessPath]
    structural_breaks: list[StructuralBreak]
```

每个 `SourceTrace`：

```json
{
  "message_id": "conv0_s01_m132",
  "processed_commit_ids": [1],
  "candidate_ids": ["cand_011"],
  "decision_ids": ["decision_011"],
  "first_memory_version_ids": ["mem_state_v1"],
  "descendant_version_ids": [
    "mem_state_v2",
    "mem_state_v3"
  ],
  "available_at_snapshot_version_ids": [
    "mem_state_v3"
  ],
  "retrieved_version_ids": [],
  "selected_evidence_ids": []
}
```

算法：

```text
FOR each Gold Message:
  1. 查 construction_inputs
  2. 查 candidate_message_edges
  3. 查 construction_decisions
  4. 查 result_version_id
  5. 查 memory_version_parent_edges 的所有 descendants
  6. 按 commit 排序版本变化
  7. 标记 snapshot 前后版本
  8. 查 access_retrieval_hits
  9. 查 access_answer_context
  10. 查 access_final_evidence
```

多个 Gold Message（如第 132、142 条）可能：

- 分别进入不同 Memory；
- 一起进入同一 Candidate；
- 后续被 MERGE；
- 在某次 UPDATE 后分叉。

因此返回的是 DAG，不假设一定是一条单链。函数同时生成一条用于展示的主路径，但完整 Parent/Child 图必须保留。

---

### 2.4 TYPE 判定的固定规则

按当前讨论，将 `TYPE1` 暂定义为 Access Failure：

```text
Raw Conversation 支持答案
AND
Snapshot Memory 在语义和时间上充分
AND
Runtime answer 时实际可见的 Memory 不充分
AND
Runtime Answer 错误
```

随后继续区分：

```text
充分 Memory 没有被返回
→ TYPE1 / Access Retrieval

已返回但没有进入 workspace
→ TYPE1 / Access Context

answer 时可见 workspace 已经充分，但答案仍错
→ Other / Answer Failure
```

因此 Failure 的第一层漏斗是：

```text
Runtime 可见 Memory 充分
→ Model / Answer Failure

Runtime 可见 Memory 不充分
但 Snapshot Store 充分
→ TYPE1 / Access Failure

Snapshot Store 不充分
但 Raw Conversation 充分
→ Construction Failure

Raw Conversation 不充分
→ Invalid
```

Memory 不充分时：

```text
→ Construction Failure
→ 进入 S6 追踪版本演化
```

Raw Conversation 不支持答案时：

```text
→ Invalid
```

这比“Message ID 在 Memory 中存在就判定 Access”更严格。存在 provenance edge 只能证明消息参与过 Memory 演化，不能证明 snapshot Memory 仍然语义充分。

---

### 2.5 Memory 充分性判断

算法先召回：

1. Gold Message 直接生成的 Memory；
2. 这些 Memory 的所有 descendants；
3. Snapshot 时有效的版本；
4. 与 Gold claim 语义相近但 provenance 缺失的 Memory。

第 4 路非常重要：

```text
source edge 不存在
但系统中存在语义充分 Memory
→ provenance_missing
```

不能错误归因为 extraction omission。

强模型一次批量判断每个 Gold Evidence Unit：

```json
{
  "evidence_unit_id": "gold_01",
  "memory_sufficiency": "FULL",
  "supporting_version_ids": ["mem_state_v3"],
  "missing_claims": [],
  "temporal_status": "CORRECT",
  "reason": "The snapshot preserves both the old and current states."
}
```

枚举：

```text
FULL
PARTIAL
CONTRADICTORY
ABSENT
```

多跳问题要求所有 `required=true` 的 Gold Evidence Unit 都达到 FULL，或者组合后足以推出答案。

---

### 2.6 版本演化定位

Memory 不充分时，不让 Failure Agent 自由搜索，而是由算法生成按 commit 排序的版本事件：

```json
{
  "memory_id": "mem_residence",
  "events": [
    {
      "commit_id": 1,
      "operation": "ADD",
      "version_id": "mem_residence_v1",
      "direct_message_ids": ["msg_132"],
      "changed_fields": {}
    },
    {
      "commit_id": 4,
      "operation": "UPDATE",
      "update_type": "state_change",
      "parent_version_ids": ["mem_residence_v1"],
      "version_id": "mem_residence_v2",
      "direct_message_ids": ["msg_142"],
      "changed_fields": {
        "object_text": {
          "before": "Boston",
          "after": "Seattle"
        }
      }
    }
  ]
}
```

然后强模型在一次调用中为每个版本标记：

```text
FULL
PARTIAL
CONTRADICTORY
ABSENT
```

算法找第一个状态下降：

```text
FULL → PARTIAL/ABSENT
  wrong_overwrite / update_loss / incorrect_merge

第一次生成就是 PARTIAL/ABSENT
  incorrect_extraction / temporal_loss

没有第一次生成
  extraction_omission / decision_skip
```

版本图存在 MERGE 时，对所有 Parent 分支分别判断，再判断 Child。

---

### 2.7 状态机工具权限

| 状态 | 允许工具 |
| --- | --- |
| S0 | `load_failure_case` |
| S1 | `read_runtime_visible_context`、`judge_context_sufficiency`、`blind_reanswer`、`score_maintenance_answer` |
| S2 | `read_gold_sources`、`read_source_window` |
| S3 | `trace_answer_sources` |
| S4 | `judge_snapshot_memory_sufficiency` |
| S5 | `inspect_access_path`、`run_oracle_retrieval` |
| S6 | `inspect_version_evolution`、`judge_version_chain` |
| S7 | `run_oracle_construction`、`run_oracle_access` |
| S8 | `finalize_failure_report` |

禁止提供：

```text
execute_sql
write_memory
update_skill
read_other_conversation
```

大部分“工具调用”实际上由 Workflow 固定执行。强模型只在 S1、S2、S4、S6 做语义判断，不需要自行规划整个调查过程。

---

## 3. 权限边界

### 3.1 可读取

- question；
- runtime prediction；
- reference answer；
- LoCoMo source message IDs；
- 原始 conversation/session/message；
- Construction 输入、Candidate 和 Decision；
- 所有相关 Memory Version；
- Memory Parent/Message lineage；
- 问题对应的 `snapshot_commit_id`；
- Access action、search filters、returned hits、visible hits；
- 最终 `access_answer_context`、Answer Prompt hash；
- final evidence IDs；
- 当次 Construction/Access Skill IDs；
- prompt/config/model hash。

### 3.2 禁止

- 修改原始 Message；
- 修改事实 Memory；
- 修改真实 Skill Bank；
- 在真实 Store 中创建“正确答案 Memory”；
- 读取其他 conversation；
- 将 snapshot 之后才产生的 Memory 当成运行时可用证据；
- 在 validation/test 阶段更新 Skill；
- 直接执行 SQL；
- 把 reference answer 写进 Skill。

### 3.3 数据访问方式

Failure Agent 不接收完整 SQLite dump，只能通过只读服务获得相关证据：

```text
ProvenanceService
MemoryInspectionService
AccessTraceReader
SourceReader
CounterfactualVerifier
```

这样既保留“有权限访问所有版本”的能力，又避免上下文失控和未来数据泄漏。

---

## 4. Failure 主分类

保持与 MiM 主设计一致：

```text
construction
access
other
invalid
```

### construction

Raw source 支持答案，但问题 snapshot 中没有充分、正确的 Memory 表达。

### access

问题 snapshot 中存在充分 Memory，但最终 Answer Prompt 中实际可见的 Memory 不充分。断点发生在检索、过滤、排序、上下文保留或 Answer Prompt 构建阶段。

### other

最终 Answer Prompt 已包含充分、正确的 Memory，但弱模型仍然答错。包括推理、指令遵循、证据选择、证据组合和答案生成错误。

### invalid

- Raw source 不支持 reference；
- source 标注错误；
- 数据损坏；
- 无法可靠判断；
- 标准答案与 conversation 冲突。

### Mixed Failure

不增加第五个主分类。使用：

```json
{
  "primary_label": "construction",
  "contributing_failures": [
    {
      "label": "access",
      "subtype": "strategy_failure"
    }
  ]
}
```

Primary 是证据链的第一个断点，后续问题作为 contributor。

---

## 5. 稳定 Message ID

LoCoMo 导入时确定性生成：

```text
<conversation_id>_<session_index>_<turn_index>
```

示例：

```text
conv_03_s05_m007
```

生成规则：

```python
def make_message_id(
    conversation_id: str,
    session_index: int,
    turn_index: int,
) -> str:
    return f"{conversation_id}_s{session_index:02d}_m{turn_index:03d}"
```

要求：

- 同一 dataset hash 下始终相同；
- 不使用随机 UUID；
- role/speaker/content 不参与 ID，避免文本清洗改变 ID；
- `content_hash` 单独保存用于检测数据变化；
- split manifest 保存 dataset hash。

---

## 6. Provenance 数据模型

### 6.1 为什么不能只用 `source_message_ids`

一个新 Memory Version 可能：

- 由当前 Message 直接生成；
- 基于旧 Memory 更新；
- 合并多个旧 Memory；
- 纠正旧 Memory；
- 保留部分旧信息并加入新信息。

因此必须区分：

```text
Direct Source
  直接支持本次版本新内容的 Message。

Parent Version
  本次版本基于哪些旧 Memory Version。

Lineage Source
  通过父版本递归得到的所有历史 Message。
```

Lineage 代表演化参与关系，不等于每条历史 Message 都直接支持当前最终内容。

---

## 7. Construction 输入记录

每个 commit 必须记录它实际处理过哪些 Message：

```sql
CREATE TABLE construction_inputs (
    commit_id   INTEGER NOT NULL,
    message_id  TEXT NOT NULL,
    PRIMARY KEY (commit_id, message_id),
    FOREIGN KEY (commit_id)
        REFERENCES construction_commits(commit_id),
    FOREIGN KEY (message_id)
        REFERENCES messages(message_id)
);

CREATE INDEX idx_construction_inputs_message
ON construction_inputs(message_id, commit_id);
```

用途：

- 判断 Message 是否进入 Construction；
- 区分 ingestion failure 与 extraction omission；
- Replay 时恢复相同输入。

---

## 8. Candidate 记录

所有 Candidate 都保存，包括最终被 SKIP 的 Candidate。

```sql
CREATE TABLE memory_candidates (
    candidate_id       TEXT PRIMARY KEY,
    commit_id          INTEGER NOT NULL,
    conversation_id    TEXT NOT NULL,
    memory_kind        TEXT NOT NULL,
    subject            TEXT NOT NULL,
    predicate          TEXT,
    object_text        TEXT,
    content            TEXT NOT NULL,
    world_start        TEXT,
    world_end          TEXT,
    entities_json      TEXT NOT NULL,
    keywords_json      TEXT NOT NULL,
    importance         REAL NOT NULL,
    confidence         REAL NOT NULL,
    content_hash       TEXT NOT NULL,
    created_at         TEXT NOT NULL,
    FOREIGN KEY (commit_id)
        REFERENCES construction_commits(commit_id)
);

CREATE TABLE candidate_message_edges (
    candidate_id  TEXT NOT NULL,
    message_id    TEXT NOT NULL,
    relation      TEXT NOT NULL,
    PRIMARY KEY (candidate_id, message_id, relation),
    FOREIGN KEY (candidate_id)
        REFERENCES memory_candidates(candidate_id),
    FOREIGN KEY (message_id)
        REFERENCES messages(message_id)
);

CREATE INDEX idx_candidate_message
ON candidate_message_edges(message_id, candidate_id);
```

第一版 `relation`：

```text
direct_support
```

Candidate 来源由 Construction Agent 输出，程序负责：

- ID 存在性；
- conversation/session 边界；
- 非空；
- 去重。

程序不使用另一个 LLM 推测来源。

---

## 9. Construction Decision 记录

```sql
CREATE TABLE construction_decisions (
    decision_id          TEXT PRIMARY KEY,
    commit_id            INTEGER NOT NULL,
    candidate_id         TEXT NOT NULL,
    decision_index       INTEGER NOT NULL,
    action               TEXT NOT NULL,
    target_memory_id     TEXT,
    update_type          TEXT,
    result_version_id    TEXT,
    reason               TEXT NOT NULL,
    validation_status    TEXT NOT NULL,
    validation_errors    TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    FOREIGN KEY (commit_id)
        REFERENCES construction_commits(commit_id),
    FOREIGN KEY (candidate_id)
        REFERENCES memory_candidates(candidate_id),
    UNIQUE (commit_id, decision_index)
);

CREATE INDEX idx_decisions_candidate
ON construction_decisions(candidate_id, action);
```

`action`：

```text
ADD
UPDATE
MERGE
SKIP
```

`validation_status`：

```text
accepted
rejected
rolled_back
```

即使 commit rollback，也保留 plan/decision artifact；数据库中只保存成功事务内的状态时，可将失败 Decision 写入 run JSONL。工程上不能因为 rollback 丢失诊断信息。

---

## 10. Memory Version 与 Message 边

规范化直接来源：

```sql
CREATE TABLE memory_version_message_edges (
    version_id   TEXT NOT NULL,
    message_id   TEXT NOT NULL,
    relation     TEXT NOT NULL,
    PRIMARY KEY (version_id, message_id, relation),
    FOREIGN KEY (version_id)
        REFERENCES memory_versions(version_id),
    FOREIGN KEY (message_id)
        REFERENCES messages(message_id)
);

CREATE INDEX idx_memory_message_edge
ON memory_version_message_edges(message_id, version_id);
```

第一版 `relation`：

```text
direct_support
```

`memory_versions.source_message_ids` 可以继续保留作为 JSON 导出缓存，但规范化 edge 表是查询与溯源的事实来源。

---

## 11. Memory Version Parent 边

`parent_version_id` 单字段不能表达 MERGE，因此增加多父关系表：

```sql
CREATE TABLE memory_version_parent_edges (
    child_version_id   TEXT NOT NULL,
    parent_version_id  TEXT NOT NULL,
    relation           TEXT NOT NULL,
    PRIMARY KEY (
        child_version_id,
        parent_version_id,
        relation
    ),
    FOREIGN KEY (child_version_id)
        REFERENCES memory_versions(version_id),
    FOREIGN KEY (parent_version_id)
        REFERENCES memory_versions(version_id)
);

CREATE INDEX idx_memory_parent
ON memory_version_parent_edges(parent_version_id, child_version_id);
```

`relation`：

```text
state_change
correction
enrichment
merge
```

规则：

- ADD：无 Parent；
- UPDATE：一个 Parent；
- MERGE：两个或多个 Parent；
- Parent 必须属于同一 conversation；
- Parent 的 `system_from_commit` 必须不晚于 Child commit；
- 不允许形成环。

---

## 12. Materialized Lineage Closure

为避免 Failure 分析时递归扫描全部版本，保存 Message 血缘闭包：

```sql
CREATE TABLE memory_lineage_messages (
    version_id         TEXT NOT NULL,
    message_id         TEXT NOT NULL,
    origin_version_id  TEXT NOT NULL,
    min_depth          INTEGER NOT NULL,
    PRIMARY KEY (
        version_id,
        message_id,
        origin_version_id
    ),
    FOREIGN KEY (version_id)
        REFERENCES memory_versions(version_id),
    FOREIGN KEY (message_id)
        REFERENCES messages(message_id),
    FOREIGN KEY (origin_version_id)
        REFERENCES memory_versions(version_id)
);

CREATE INDEX idx_lineage_message
ON memory_lineage_messages(message_id, version_id);
```

### 12.1 生成算法

新版本 `child` 写入同一事务时：

```text
1. 对 child 的 direct source:
   origin_version_id = child
   min_depth = 0

2. 对每个 parent:
   复制 parent 的 lineage rows
   version_id = child
   min_depth = parent.min_depth + 1

3. 相同 version/message/origin:
   保留最小 depth
```

伪代码：

```python
def build_lineage(
    child_version_id: str,
    direct_message_ids: set[str],
    parent_version_ids: list[str],
) -> list[LineageEdge]:
    edges = {}

    for message_id in direct_message_ids:
        key = (child_version_id, message_id, child_version_id)
        edges[key] = 0

    for parent_id in parent_version_ids:
        for edge in store.get_lineage(parent_id):
            key = (
                child_version_id,
                edge.message_id,
                edge.origin_version_id,
            )
            depth = edge.min_depth + 1
            edges[key] = min(edges.get(key, depth), depth)

    return [
        LineageEdge(*key, min_depth=depth)
        for key, depth in edges.items()
    ]
```

整个过程纯算法，不调用 LLM。

---

## 13. Change Event 与字段 Diff

每次 Memory 改变都保存：

```sql
CREATE TABLE memory_change_events (
    change_id            TEXT PRIMARY KEY,
    commit_id            INTEGER NOT NULL,
    decision_id          TEXT NOT NULL,
    operation            TEXT NOT NULL,
    new_version_id       TEXT,
    changed_fields_json  TEXT NOT NULL,
    direct_message_ids   TEXT NOT NULL,
    affected_message_ids TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    FOREIGN KEY (commit_id)
        REFERENCES construction_commits(commit_id),
    FOREIGN KEY (decision_id)
        REFERENCES construction_decisions(decision_id)
);

CREATE TABLE memory_change_parents (
    change_id          TEXT NOT NULL,
    parent_version_id  TEXT NOT NULL,
    PRIMARY KEY (change_id, parent_version_id),
    FOREIGN KEY (change_id)
        REFERENCES memory_change_events(change_id),
    FOREIGN KEY (parent_version_id)
        REFERENCES memory_versions(version_id)
);
```

### 13.1 Diff 字段

纯算法比较：

```text
memory_kind
subject
predicate
object_text
content
world_start
world_end
entities
keywords
direct sources
```

忽略：

```text
embedding
recorded_at
commit ID
model metadata
trace metadata
```

输出：

```json
{
  "object_text": {
    "before": "Boston",
    "after": "Seattle"
  },
  "world_start": {
    "before": null,
    "after": "2023-05"
  }
}
```

### 13.2 affected_message_ids

```text
affected =
    new direct source messages
    ∪ all parent lineage messages
```

这表示本次变更影响了哪些历史来源，不表示所有历史来源都支持新版本内容。

---

## 14. SKIP 和 Extraction Omission

Failure 需要区分：

### Message 未进入 Construction

```text
construction_inputs 中不存在
→ ingestion_failure
```

### Message 已处理但没有 Candidate

```text
construction_inputs 存在
candidate_message_edges 不存在
→ extraction_omission
```

### Candidate 被 SKIP

```text
Candidate 存在
Decision.action = SKIP
→ decision_skip
```

### Decision 存在但 Commit 失败

```text
Decision 存在
commit.status = failed / rolled_back
→ persistence_failure
```

这些判断完全由算法完成，不需要 LLM。

---

## 15. Access Trace 规范化

只保存一个大 JSON 不利于查询。建议核心字段进入 SQLite，完整 prompt/observation 仍保存在 JSONL。

### 15.1 Access Run

```sql
CREATE TABLE access_runs (
    access_run_id        TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    conversation_id      TEXT NOT NULL,
    qa_id                TEXT NOT NULL,
    snapshot_commit_id   INTEGER NOT NULL,
    question             TEXT NOT NULL,
    prediction           TEXT,
    skill_version_ids    TEXT NOT NULL,
    answer_prompt_hash   TEXT,
    status               TEXT NOT NULL,
    created_at           TEXT NOT NULL,
    completed_at         TEXT
);

CREATE INDEX idx_access_run_qa
ON access_runs(run_id, conversation_id, qa_id);
```

### 15.2 Access Action

```sql
CREATE TABLE access_actions (
    action_id       TEXT PRIMARY KEY,
    access_run_id   TEXT NOT NULL,
    step_index      INTEGER NOT NULL,
    action_type     TEXT NOT NULL,
    request_json    TEXT NOT NULL,
    response_json   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    FOREIGN KEY (access_run_id)
        REFERENCES access_runs(access_run_id),
    UNIQUE (access_run_id, step_index)
);
```

`action_type`：

```text
search_memory
inspect_memory
answer
```

### 15.3 Retrieval Hit

```sql
CREATE TABLE access_retrieval_hits (
    action_id          TEXT NOT NULL,
    version_id         TEXT NOT NULL,
    raw_rank           INTEGER NOT NULL,
    final_rank         INTEGER,
    semantic_rank      INTEGER,
    keyword_rank       INTEGER,
    structured_rank    INTEGER,
    fused_score        REAL,
    returned_to_agent  INTEGER NOT NULL,
    kept_in_workspace  INTEGER NOT NULL,
    PRIMARY KEY (action_id, version_id),
    FOREIGN KEY (action_id)
        REFERENCES access_actions(action_id),
    FOREIGN KEY (version_id)
        REFERENCES memory_versions(version_id)
);

CREATE INDEX idx_access_hit_version
ON access_retrieval_hits(version_id, action_id);
```

### 15.4 Final Answer-visible Context

```sql
CREATE TABLE access_answer_context (
    access_run_id   TEXT NOT NULL,
    version_id      TEXT NOT NULL,
    context_index   INTEGER NOT NULL,
    rendered_text   TEXT NOT NULL,
    token_count     INTEGER,
    PRIMARY KEY (access_run_id, context_index),
    UNIQUE (access_run_id, version_id),
    FOREIGN KEY (access_run_id)
        REFERENCES access_runs(access_run_id),
    FOREIGN KEY (version_id)
        REFERENCES memory_versions(version_id)
);
```

该表在最终 `answer` 调用前一次性写入，记录真正序列化进 Prompt 的 Memory，而不是中间 workspace 的近似状态。完整 Answer Prompt 仍保存到 trace JSONL；`access_runs.answer_prompt_hash` 用于校验 Bundle 中的上下文与当时 Prompt 一致。

如果同一个 Memory Version 在 Prompt 中因分段而出现多次，应先按 Version 聚合为一个 `rendered_text`，或把主键扩展为 `(access_run_id, context_index)` 并取消唯一 Version 约束；MVP 推荐前者，避免诊断重复计数。

### 15.5 Final Evidence

```sql
CREATE TABLE access_final_evidence (
    access_run_id  TEXT NOT NULL,
    version_id     TEXT NOT NULL,
    evidence_index INTEGER NOT NULL,
    PRIMARY KEY (access_run_id, version_id),
    FOREIGN KEY (access_run_id)
        REFERENCES access_runs(access_run_id),
    FOREIGN KEY (version_id)
        REFERENCES memory_versions(version_id)
);
```

这样可以精确区分：

```text
召回池存在
→ 是否返回 Agent
→ 是否进入最终 Answer Prompt
→ 是否进入最终 evidence
```

Failure 第一步读取 `access_answer_context`。`access_retrieval_hits.kept_in_workspace` 只用于解释上下文如何变化，不能代替最终可见上下文。

---

## 16. QA Gold Source

```sql
CREATE TABLE qa_cases (
    qa_id              TEXT PRIMARY KEY,
    conversation_id    TEXT NOT NULL,
    category           INTEGER,
    question           TEXT NOT NULL,
    reference_answer   TEXT NOT NULL,
    metadata_json      TEXT NOT NULL
);

CREATE TABLE qa_gold_sources (
    qa_id       TEXT NOT NULL,
    message_id  TEXT NOT NULL,
    PRIMARY KEY (qa_id, message_id),
    FOREIGN KEY (qa_id)
        REFERENCES qa_cases(qa_id),
    FOREIGN KEY (message_id)
        REFERENCES messages(message_id)
);
```

LoCoMo 提供 source 时直接映射为 `qa_gold_sources`。

如果 source 缺失：

1. 对当前 conversation 原始 Message 做 FTS/semantic search；
2. Failure Agent 从候选中选择 source；
3. 标记 `source_origin=agent_inferred`；
4. 降低归因置信度；
5. 默认不自动学习 Skill，除非 counterfactual 明确通过。

---

## 17. Failure Case

错误 QA 触发时立即冻结：

```sql
CREATE TABLE failure_cases (
    failure_id           TEXT PRIMARY KEY,
    run_id               TEXT NOT NULL,
    access_run_id        TEXT NOT NULL,
    qa_id                TEXT NOT NULL,
    conversation_id      TEXT NOT NULL,
    snapshot_commit_id   INTEGER NOT NULL,
    prediction           TEXT NOT NULL,
    reference_answer     TEXT NOT NULL,
    metric_json          TEXT NOT NULL,
    config_hash          TEXT NOT NULL,
    prompt_hashes_json   TEXT NOT NULL,
    database_hash        TEXT NOT NULL,
    status               TEXT NOT NULL,
    created_at           TEXT NOT NULL
);
```

`failure_cases` 位于独立 `failure.sqlite3` 时不能对 Runtime 的 `access_runs` 建跨数据库 Foreign Key。`access_run_id`、`database_hash` 和冻结的 `FailureInputBundle` 共同保证引用稳定；Provider 加载时显式校验目标记录存在。

`status`：

```text
pending
tracing
attributing
verifying
completed
review_required
failed
```

冻结后不允许用后续 run 的 Memory/trace 替换。

---

## 18. Gold Evidence Unit

标准答案可能依赖多个原子证据。Failure Agent 首先生成：

```json
{
  "evidence_unit_id": "gold_01",
  "claim": "Alice lived in Boston before May 2023.",
  "source_message_ids": ["conv0_s01_m002"],
  "required": true,
  "target_time_mode": "before",
  "target_time": "2023-05"
}
```

多跳示例：

```text
gold_01: Alice works for Company X.
gold_02: Company X is located in Berlin.
answer: Berlin.
```

### 18.1 工程实现

`GoldEvidenceBuilder` 使用 maintenance 强模型完成语义拆分，因为算法无法从任意自然语言答案可靠推导原子 claim。

但是：

- 只能引用 `qa_gold_sources` 中的 Message；
- 每个 evidence unit 至少一个 source；
- Python 校验 source；
- 不生成任何 Memory；
- 输出保存供复查。

如果标准答案本身就是单一短事实，可以使用一个 Evidence Unit，不强制多拆。

---

## 19. Provenance Graph

### 19.1 Node

```text
MessageNode
CandidateNode
DecisionNode
MemoryVersionNode
AccessActionNode
RetrievalHitNode
AnswerContextNode
AnswerNode
```

### 19.2 Edge

```text
MESSAGE_TO_CANDIDATE
CANDIDATE_TO_DECISION
DECISION_TO_VERSION
VERSION_TO_VERSION
VERSION_TO_RETRIEVAL
RETRIEVAL_TO_WORKSPACE
WORKSPACE_TO_ANSWER_CONTEXT
WORKSPACE_TO_EVIDENCE
ANSWER_CONTEXT_TO_ANSWER
EVIDENCE_TO_ANSWER
```

### 19.3 构建方式

从 Gold Message IDs 开始：

```text
qa_gold_sources
→ construction_inputs
→ candidate_message_edges
→ construction_decisions
→ result_version_id
→ memory_version_parent_edges 的 descendants
→ snapshot 中相关 versions
→ access_retrieval_hits
→ access_answer_context
→ access_final_evidence
```

全部由 SQL/图遍历完成。

### 19.4 Snapshot 约束

对于问题的 `snapshot_commit_id = c`：

- `system_from_commit > c` 的版本标记 `future`；
- future 可以显示在完整 lineage audit 中；
- future 绝不能计入 Memory Coverage；
- future 绝不能作为 Oracle Access evidence；
- descendant 在 c 之后生成时只能帮助解释后续发生了什么，不能改变当时归因。

---

## 20. ProvenanceService

```python
class ProvenanceService:
    def trace_gold_messages(
        self,
        *,
        conversation_id: str,
        message_ids: list[str],
        snapshot_commit_id: int,
        access_run_id: str,
    ) -> ProvenanceGraph:
        ...

    def trace_memory_version(
        self,
        *,
        version_id: str,
        snapshot_commit_id: int,
    ) -> MemoryLineage:
        ...

    def find_versions_for_messages(
        self,
        *,
        message_ids: list[str],
        snapshot_commit_id: int,
        include_descendants: bool = True,
    ) -> list[MemoryVersionRef]:
        ...

    def get_access_path(
        self,
        *,
        access_run_id: str,
        version_ids: list[str],
    ) -> list[AccessPath]:
        ...
```

该服务：

- 使用只读 SQLite connection；
- 所有查询强制 conversation filter；
- 不调用模型；
- 返回 Pydantic schema；
- 记录每个查询和返回 node 数量。

---

## 21. Coverage Matrix

每个 Gold Evidence Unit 一行：

| 字段 | 含义 |
| --- | --- |
| `raw_supported` | source 是否支持该 claim |
| `message_processed` | Gold Message 是否进入 Construction |
| `candidate_created` | 是否产生相关 Candidate |
| `decision_accepted` | Candidate 是否成功写入/更新 |
| `memory_supported` | snapshot Memory 是否充分表达 claim |
| `retrieval_returned` | 正确 version 是否被工具返回 |
| `answer_context_visible` | 正确 version 是否实际进入最终 Answer Prompt |
| `runtime_context_supported` | 最终 Answer Prompt 中的 Memory 是否在语义上充分支持 claim |
| `answer_correct` | runtime answer 是否正确 |

另保留 `workspace_seen`、`evidence_selected` 作为调试字段，但它们不是 Primary 归因边界：

- `workspace_seen=true` 不代表 Answer 时仍可见；
- `evidence_selected=false` 但 `runtime_context_supported=true` 时，错误属于模型/Answer，不属于 Access。

Case 级别的 `runtime_context_sufficiency` 由全部 required Evidence Unit 聚合为 `FULL/PARTIAL/CONTRADICTORY/ABSENT`。

示例：

```json
{
  "evidence_unit_id": "gold_01",
  "raw_supported": true,
  "message_processed": true,
  "candidate_created": true,
  "decision_accepted": true,
  "memory_supported": true,
  "retrieval_returned": false,
  "answer_context_visible": false,
  "runtime_context_supported": false,
  "answer_correct": false,
  "supporting_version_ids": ["mem_residence_v1"]
}
```

---

## 22. 哪些字段由算法确定

纯算法：

```text
message_processed
candidate_created
decision_accepted
retrieval_returned
answer_context_visible
answer_correct（来自 metric）
```

强模型语义判断：

```text
raw_supported
memory_supported
runtime_context_supported
```

这种拆分避免让强模型主观编造整条执行链。

---

## 23. FailureAgent 语义判断

### 23.1 两次隔离的输入

第一阶段对应 S1，包含两个隔离调用：

- `judge_context_sufficiency`：输入 question、reference 和 `access_answer_context`；
- `blind_reanswer`：只输入 question、Runtime answer contract 和同一份 `access_answer_context`，不得输入 reference/prediction。

两者都保持 Memory 原顺序和原文本，并携带 version ID、world time、Answer Prompt hash。

绝不输入 Raw Message、snapshot 中未展示的 Memory、Candidate/Decision 或 lineage。

第二阶段仅在需要继续溯源时执行，再按状态分别输入：

- S2：Gold source messages 和必要上下文；
- S4：snapshot 中候选 Memory；
- S6：相关 Memory Version 及 diff/lineage。

不同阶段输入不能合并成一个大 Prompt，否则强模型会利用弱模型当时看不到的信息，破坏第一步的因果判定。

### 23.2 输出

```json
{
  "runtime_context_sufficiency": "FULL",
  "supporting_visible_version_ids": ["mem_residence_v3"],
  "missing_claims": [],
  "maintenance_answer": "Seattle",
  "maintenance_answer_correct": true,
  "evidence_units": [
    {
      "evidence_unit_id": "gold_01",
      "runtime_context_supported": true,
      "supporting_visible_version_ids": ["mem_residence_v3"],
      "reason": "The answer-visible memory explicitly states the current residence."
    }
  ],
  "semantic_confidence": 0.93,
  "missing_information_requests": []
}
```

### 23.3 可选只读调查

默认一次语义判断。如果模型认为 Bundle 不足，只允许请求：

```text
READ_SOURCE_WINDOW
READ_MEMORY_LINEAGE
READ_ACCESS_ACTION
SEARCH_RELATED_MEMORY
```

最多三次，不允许任意 SQL。

---

## 24. First Break Locator

`First Break` 描述正向因果链中的最早断点；实际执行仍先做 S1，以最快识别模型失败。Raw 验证通过且 S1 未结束后，再按 Evidence Unit 从左到右检查：

```text
raw_supported
→ message_processed
→ candidate_created
→ decision_accepted
→ memory_supported
→ retrieval_returned
→ answer_context_visible
→ runtime_context_supported
→ answer_correct
```

不能机械地对所有字段取第一个 `false`。Locator 必须先执行以下门控：

```python
if not raw_supported:
    return invalid("source_invalid")

if runtime_context_sufficiency == "FULL":
    if maintenance_answer_correct:
        return other("model_failure")
    return review_required("semantic_judge_inconsistent")

if not memory_supported:
    return first_construction_break()

if not retrieval_returned:
    return access("retrieval_failure")

if not answer_context_visible:
    return access("context_failure")

return access("context_coverage_or_conflict")
```

最后一项表示单条正确 Memory 可能可见，但多跳所需的其他 Memory 缺失、被截断或错误版本同时进入上下文。若全部必要信息实际已经可见，则 S1 必须判为 `FULL`，错误回到 `model_failure`。

### 24.1 映射

| 第一个断点 | Label | Subtype |
| --- | --- | --- |
| raw_supported | invalid | source_invalid |
| message_processed | construction | ingestion_failure |
| candidate_created | construction | extraction_omission |
| decision_accepted | construction | decision_or_persistence_failure |
| memory_supported | construction | memory_representation_failure |
| retrieval_returned | access | retrieval_failure |
| answer_context_visible | access | context_failure |
| runtime_context_supported | access | context_coverage_or_conflict |
| runtime context FULL 但 answer 错 | other | model_failure |

如果多个 Evidence Unit 在不同阶段断裂：

- 最早断点为 Primary；
- 其他断点进入 contributors；
- Skill 学习按 Primary 先修复，再 Replay 检查 contributor。

---

## 25. Construction Subtype

算法优先确定：

```text
ingestion_failure
extraction_omission
decision_skip
persistence_failure
provenance_missing
```

结合语义和 version diff 确定：

```text
incorrect_extraction
temporal_loss
wrong_overwrite
incorrect_merge
correction_failure
update_loss
```

规则示例：

### temporal_loss

- source 有明确时间；
- Candidate/Memory 的 `world_start/world_end` 缺失或错误；
- content 其他部分基本正确。

### wrong_overwrite

- Gold Message 曾对应正确旧版本；
- 后续 UPDATE 关闭旧版本；
- 新版本没有保留历史状态；
- 历史问题在 snapshot 中无法恢复。

### incorrect_merge

- 新版本有多个 Parent；
- Parent 分别支持不同事实；
- merged content 丢失或混淆必要 claim。

### provenance_missing

- Memory content 可能正确；
- 但没有 direct source edge；
- 或引用其他 conversation/source。

---

## 26. Access Subtype

### 26.1 query_failure

- 正确 Memory 存在；
- actual queries 没有覆盖关键实体/关系；
- Oracle query 能召回。

### 26.2 strategy_failure

- 正确 Memory 只在 temporal/keyword/structured 某路径稳定出现；
- Agent 没使用适当 strategy 或 `include_history`。

### 26.3 filter_failure

- 正确 Memory 在未过滤候选中；
- 被错误 entity/time/kind filter 排除。

### 26.4 ranking_failure

- 正确 version 进入某一路候选；
- final rank 超过返回 top-k；
- 没有返回 Agent。

### 26.5 index_failure

- Memory 正确存在；
- 使用 Gold claim 对全部策略查询仍无法召回；
- embedding/FTS/metadata 索引可能错误。

这类问题通常不应学习自然语言 Skill，应路由为工程错误。

### 26.6 context_failure

- Tool 已返回；
- `returned_to_agent=true` 但之后被 workspace eviction；
- 或返回结果未加入 prompt。

同样优先视为工程/上下文管理错误。

### 26.7 context_coverage_or_conflict

- Snapshot Store 中各条必要 Memory 都存在；
- 最终 Answer Prompt 只包含其中一部分，或混入了错误时间版本造成冲突；
- 因而 S1 为 `PARTIAL/CONTRADICTORY`。

如果 Runtime 有显式的“检索结果 → 上下文选择器”，可继续细分为 `selection_context_failure`；它指选择器在 Answer Prompt 形成前排除了必要 Memory，而不是弱模型看见充分信息后没有采用它。

### 26.8 `selection_failure` / `composition_failure` 的兼容处理

旧报告中的两个标签不再作为 Access Primary：

```text
全部必要 Memory 已进入最终 Answer Prompt
但弱模型没有选择或组合
→ other / model_failure

必要 Memory 在进入最终 Answer Prompt 前被选择器移除
→ access / context_failure 或 selection_context_failure

多跳所需 Memory 没有全部进入最终 Answer Prompt
→ access / context_coverage_or_conflict
```

这条边界与 Access & Answer 是否由同一个 Agent 实现无关；归因依据是信息是否越过“最终 Answer Prompt 可见性边界”。

### 26.9 premature_stop

- evidence 不充分；
- budget 尚未耗尽；
- Agent 直接执行 answer。

若提前停止导致必要 Memory 从未被检索/加入最终上下文，它仍是 Access Failure；若上下文已经充分，只是模型过早作答，则是 `model_failure`。

---

## 27. Oracle Retrieval Probe

为细分 Access Failure，程序对正确 Memory 运行诊断查询：

```text
semantic-only
keyword-only
structured-only
temporal/include-history
hybrid
```

查询文本：

- Gold Evidence Unit claim；
- Gold entities；
- Gold target time。

结果只用于诊断，不进入正常 Access trace。

判断：

```text
某策略能召回，实际没使用
→ strategy_failure

无过滤能召回，有过滤不能
→ filter_failure

候选池存在但 Top-k 外
→ ranking_failure

所有策略都不能召回
→ index_failure 或 Memory 表达问题
```

该过程纯算法调用现有 Retriever，不使用额外 LLM。

---

## 28. Counterfactual Verification

### 28.1 Oracle Construction

适用：

- Raw 支持；
- Memory 不支持；
- Primary 候选为 Construction。

步骤：

1. 创建临时 Store；
2. 从 Gold Evidence Unit 创建 `diagnostic_only` Oracle Memory；
3. 使用相同 Access Agent、runtime model、prompt、Skill 和预算；
4. 重新回答；
5. 不写真实数据库。

结果：

```text
回答修复
→ Construction 因果证据增强

仍失败
→ 存在 Access/Other contributor
```

### 28.2 Oracle Access

适用：

- snapshot 中存在正确 Memory；
- Primary 候选为 Access。

步骤：

1. 固定原 snapshot；
2. 将正确 Memory Version 强制加入 EvidenceWorkspace；
3. 使用相同 runtime model；
4. 不允许额外搜索；
5. 重新 answer。

结果：

```text
回答修复
→ Access 因果证据增强

仍失败
→ Other/Answer contributor
```

### 28.3 禁止泄漏

Oracle：

- 仅 train failure；
- 使用临时 artifact；
- 不进入 Skill Bank；
- 不进入正式 Base/MiM 指标；
- 结果标记 `diagnostic_only=true`。

### 28.4 Counterfactual Runtime 契约

Failure 核心不 import Runtime Workflow，而是调用窄协议：

```python
class CounterfactualRunner(Protocol):
    def replay_with_oracle_memory(
        self,
        case: FailureCase,
        oracle_memories: list[DiagnosticMemory],
    ) -> CounterfactualResult:
        ...

    def answer_with_forced_evidence(
        self,
        case: FailureCase,
        evidence_version_ids: list[str],
    ) -> CounterfactualResult:
        ...
```

Runtime 项目提供 adapter；Failure 单元测试使用 Mock 实现。协议只返回结果和 trace，不暴露可写 Memory Store。

---

## 29. Failure Report

```json
{
  "failure_id": "failure_conv2_qa14",
  "run_id": "train_mim_001",
  "conversation_id": "conv2",
  "qa_id": "qa14",
  "snapshot_commit_id": 12,

  "question": "...",
  "prediction": "...",
  "reference_answer": "...",

  "primary_label": "access",
  "primary_subtype": "strategy_failure",
  "first_broken_edge": "memory_to_retrieval",
  "confidence": 0.94,

  "runtime_context_check": {
    "sufficiency": "PARTIAL",
    "answer_prompt_hash": "sha256:...",
    "visible_version_ids": ["mem_0012_v1"],
    "supporting_visible_version_ids": [],
    "missing_claims": ["current residence"],
    "maintenance_answer": "...",
    "maintenance_answer_correct": false
  },

  "contributing_failures": [],
  "gold_evidence_units": [],
  "coverage_matrix": [],

  "raw_message_ids": ["conv2_s04_m007"],
  "candidate_ids": ["cand_021"],
  "decision_ids": ["decision_021"],
  "relevant_memory_version_ids": ["mem_0012_v1"],
  "access_action_ids": ["access_action_03"],

  "version_diffs": [],
  "oracle_retrieval_probes": {},
  "counterfactuals": {
    "oracle_construction": "not_needed",
    "oracle_access": "passed"
  },

  "recommended_route": "access_skill_maker",
  "failure_signature": "historical_state_without_history_search",
  "review_required": false
}
```

报告中的每个 ID 必须能查询到原始记录。

---

## 30. Learning Router

```python
class LearningRouter:
    def route(self, report: FailureReport) -> LearningRoute:
        if report.review_required:
            return LearningRoute.RECORD_ONLY

        if report.confidence < self.min_confidence:
            return LearningRoute.RECORD_ONLY

        if report.primary_label == "construction":
            return LearningRoute.CONSTRUCTION_SKILL_MAKER

        if report.primary_label == "access":
            if report.primary_subtype in {
                "index_failure",
                "context_failure",
            }:
                return LearningRoute.ENGINEERING_ISSUE
            return LearningRoute.ACCESS_SKILL_MAKER

        return LearningRoute.RECORD_ONLY
```

默认自动学习条件：

```yaml
failure:
  auto_learn_min_confidence: 0.80
  require_valid_gold_source: true
  require_complete_coverage: true
  require_counterfactual_for_auto_learn: true
```

---

## 31. 工程问题与 Skill 问题分开

不是所有 Construction/Access 错误都适合学习 Skill。

### 适合 Skill

```text
信息更新时应保留历史状态
历史问题应 include_history
多跳问题应拆分查询
证据不足时不应过早停止
```

### 不适合 Skill

```text
FTS 索引未更新
embedding 维度错误
SQLite commit rollback bug
workspace 代码漏放结果
source ID 跨 conversation
trace 缺失
```

FailureReport 必须包含：

```text
recommended_route:
  construction_skill_maker
  access_skill_maker
  engineering_issue
  record_only
```

这能避免 Skill Bank 被工程 Bug 污染。

---

## 32. FailureWorkflow 接口

```python
class FailureWorkflow:
    def analyze(
        self,
        *,
        failure_case: FailureCase,
        store: ReadOnlyMemoryStore,
        trace_reader: AccessTraceReader,
        counterfactual_runner: CounterfactualRunner,
    ) -> FailureReport:
        ...
```

内部：

```python
def analyze(...):
    self.guard.assert_train_phase(failure_case)
    self.guard.assert_snapshot_frozen(failure_case)

    # S1：第一步只看弱模型最终真正看到的 Memory。
    answer_context = trace_reader.get_answer_context(
        access_run_id=failure_case.access_run_id,
    )
    self.guard.assert_prompt_hash_matches(
        answer_context,
        failure_case.answer_prompt_hash,
    )
    context_judgment = self.failure_agent.judge_context_sufficiency(
        question=failure_case.question,
        reference_answer=failure_case.reference_answer,
        visible_memories=answer_context.memories,
    )
    maintenance_answer = self.failure_agent.blind_reanswer(
        question=failure_case.question,
        answer_contract=failure_case.answer_contract,
        visible_memories=answer_context.memories,
    )
    maintenance_correct = self.answer_metric.score(
        prediction=maintenance_answer,
        reference=failure_case.reference_answer,
    ).passed
    context_result = RuntimeContextResult.combine(
        context_judgment,
        maintenance_answer,
        maintenance_correct,
    )

    # S2：独立输入验证 Gold/Reference，不能把 Raw 泄漏给 S1。
    raw_result = self.failure_agent.verify_raw_evidence(
        question=failure_case.question,
        reference_answer=failure_case.reference_answer,
        source_messages=store.get_gold_source_window(failure_case),
    )
    if raw_result.is_invalid:
        return self.report_builder.invalid_source(
            failure_case, context_result, raw_result
        )

    if context_result.is_full:
        if context_result.maintenance_answer_correct:
            return self.report_builder.model_failure(
                failure_case,
                context_result=context_result,
                raw_result=raw_result,
                recommended_route="record_only",
            )
        return self.report_builder.review_required(
            failure_case,
            reason="semantic_judge_inconsistent",
            context_result=context_result,
            raw_result=raw_result,
        )

    # 只有最终可见 Memory 不充分时，才构建完整 provenance 并检查上游。
    gold_units = self.gold_builder.build(failure_case)

    graph = self.provenance.trace_gold_messages(
        conversation_id=failure_case.conversation_id,
        message_ids=collect_sources(gold_units),
        snapshot_commit_id=failure_case.snapshot_commit_id,
        access_run_id=failure_case.access_run_id,
    )

    algorithmic_coverage = self.coverage.from_graph(
        gold_units,
        graph,
    )

    snapshot_result = self.failure_agent.judge_snapshot_memory(
        failure_case=failure_case,
        gold_units=gold_units,
        snapshot_memories=graph.snapshot_memories,
    )

    matrix = self.coverage.combine(
        algorithmic_coverage,
        context_result,
        raw_result,
        snapshot_result,
    )

    hypothesis = self.locator.locate(matrix, graph)
    subtype = self.subtype_classifier.classify(
        hypothesis,
        graph,
    )

    probes = self.oracle_retrieval.run_if_needed(
        failure_case,
        gold_units,
        graph,
        subtype,
    )

    counterfactual = self.counterfactual.verify(
        failure_case,
        gold_units,
        graph,
        subtype,
    )

    return self.report_builder.build(
        failure_case,
        gold_units,
        graph,
        matrix,
        subtype,
        probes,
        counterfactual,
    )
```

---

## 33. 只读 Store

```python
class ReadOnlyMemoryStore(Protocol):
    def get_messages(
        self,
        conversation_id: str,
        message_ids: list[str],
    ) -> list[Message]:
        ...

    def get_source_window(
        self,
        conversation_id: str,
        message_id: str,
        radius: int = 2,
    ) -> list[Message]:
        ...

    def get_versions_by_messages(
        self,
        conversation_id: str,
        message_ids: list[str],
        snapshot_commit_id: int,
    ) -> list[MemoryVersion]:
        ...

    def get_version_lineage(
        self,
        conversation_id: str,
        version_id: str,
    ) -> MemoryLineage:
        ...

    def get_snapshot_versions(
        self,
        conversation_id: str,
        snapshot_commit_id: int,
        include_history: bool,
    ) -> list[MemoryVersion]:
        ...
```

建议使用：

```sql
PRAGMA query_only = ON;
```

并为 Failure Workflow 单独创建只读 connection。

---

## 34. 文件树

```text
src/mim/
├─ agents/
│  └─ failure.py
├─ failure/
│  ├─ __init__.py
│  ├─ schemas.py
│  ├─ case_builder.py
│  ├─ gold_evidence.py
│  ├─ provenance.py
│  ├─ coverage.py
│  ├─ first_break.py
│  ├─ subtype.py
│  ├─ oracle_retrieval.py
│  ├─ counterfactual.py
│  ├─ router.py
│  ├─ report.py
│  └─ workflow.py
├─ storage/
│  ├─ schema.sql
│  └─ sqlite_store.py
└─ tracing.py
```

职责：

- `agents/failure.py`：强模型语义判断；
- `failure/provenance.py`：纯算法图；
- `failure/coverage.py`：Coverage Matrix；
- `failure/first_break.py`：第一个断点；
- `failure/subtype.py`：细分类；
- `failure/counterfactual.py`：Oracle Replay；
- `failure/router.py`：Skill/Engineering 路由；
- `failure/workflow.py`：总编排。

不需要单独服务或队列。

---

## 35. 配置

```yaml
failure:
  enabled_phases:
    - train

  max_gold_evidence_units: 6
  max_related_memory_versions: 50
  max_source_window_radius: 2
  max_read_tool_calls: 3

  auto_learn_min_confidence: 0.80
  require_valid_gold_source: true
  require_complete_coverage: true
  require_counterfactual_for_auto_learn: true

  run_oracle_retrieval: true
  run_oracle_construction: true
  run_oracle_access: true

  engineering_subtypes:
    - index_failure
    - context_failure
    - persistence_failure
    - provenance_missing
```

Base/MiM Failure Attribution 评估必须使用同一配置。

---

## 36. 性能与上下文优化

### 36.1 Materialized Lineage

写入时构建 lineage closure，Failure 查询不递归扫描所有祖先。

### 36.2 先算法后 LLM

不向强模型发送无关数据：

```text
Gold sources
→ 相关 Candidate/Decision
→ 相关版本和 descendants
→ 对应 Access events
```

通常只需几十条记录，而不是完整 conversation + 全 Memory。

### 36.3 Batch Semantic Judgment

一次判断多个 Gold Evidence Unit，避免每个 unit 单独调用强模型。

### 36.4 Counterfactual 按需运行

- invalid 不运行；
- other 通常只需 Oracle Access；
- clear engineering issue 不运行 Skill Counterfactual；
- 低置信度 source 先 review。

### 36.5 Bundle Cache

缓存键：

```text
failure_case_hash
+ database_hash
+ snapshot_commit_id
+ prompt_hash
```

重复调试不重新构建 Provenance Graph。

---

## 37. 可优化点与设计思考

### 37.1 Direct Source 与 Lineage 必须分离

这是最重要的设计。

如果新版本简单继承所有旧 Message 并统一标为 `source`：

- Correction 后错误旧消息会被误认为支持新内容；
- State Change 后历史消息会被误认为支持当前状态；
- Failure Agent 无法区分“直接支持”和“参与演化”。

因此：

```text
direct support edge
parent version edge
lineage closure
```

必须分别保存。

### 37.2 Provenance 不应只存在 JSON

JSON trace 适合审计，但不适合稳定 join 和反向查询。

核心边进入 SQLite：

- Message → Candidate；
- Candidate → Decision；
- Decision → Version；
- Version → Version；
- Version → Retrieval；
- Retrieval → Evidence。

完整 Prompt/Observation 再放 JSONL。

### 37.3 不让 LLM 自己“回忆执行过程”

Failure Agent 不应该阅读日志后自由叙述“可能发生了什么”。执行路径必须由算法先恢复，LLM 只判断：

- Raw 是否支持 claim；
- Memory 是否表达 claim；
- Evidence 是否足以回答。

### 37.4 Primary + Contributor 比 Mixed Label 更好

Flat `mixed` 无法决定先修哪一侧。

使用：

```text
primary = first broken edge
contributors = counterfactual 后仍存在的问题
```

先修 Primary，再 Replay；只有仍失败才学习 Contributor。

### 37.5 Failure Agent 不能看到未来

拥有所有版本的“审计权限”不等于可以把未来版本作为当时可用证据。

任何 Version 必须携带：

```text
available_at_snapshot
future_to_snapshot
```

未来 Version 只能解释 lineage，不能参与 Memory Coverage 和 Oracle Access。

### 37.6 Source ID 仍可能语义不准

Candidate 的 source ID 来自 Construction Agent，算法只能验证 ID 合法，不能证明该 Message 真正支持内容。

因此 Failure Agent 仍需判断 `raw_supported` 和 `memory_supported`。如果以后要进一步提高精度，可以让 Candidate 同时输出：

```text
message_id
supporting_quote
```

程序校验 quote 是否为原文子串。第一版不强制字符 offset，避免 Unicode/清洗导致偏移错误。

### 37.7 Index Failure 不应进入 Skill Bank

如果 Gold claim 使用全部策略仍无法召回正确 Memory，问题可能是：

- embedding；
- FTS；
- metadata；
-索引同步。

给 Access Agent 写“更认真搜索”的 Skill 没有意义，应输出 engineering issue。

### 37.8 Failure Report 本身必须版本化

归因 Prompt 或算法变化后，同一 Failure 可能得到不同报告。

建议：

```text
failure_id
attribution_version
parent_report_id
algorithm_version
prompt_hash
model
```

第一版可以采用文件版本：

```text
failure_<id>_report_v001.json
failure_<id>_report_v002.json
```

---

## 38. 测试

### 38.1 Provenance Unit Tests

- ADD：direct 与 lineage 相同；
- UPDATE：child 包含 parent lineage；
- MERGE：child 包含多个 parent lineage；
- Correction：旧 source 是 lineage，不是 child direct source；
- parent cycle 被拒绝；
-跨 conversation parent 被拒绝。

### 38.2 Construction Failure Fixtures

```text
Message 未处理
→ ingestion_failure

Message 有输入但无 Candidate
→ extraction_omission

Candidate 被 SKIP
→ decision_skip

Commit rollback
→ persistence_failure

历史版本被错误覆盖
→ wrong_overwrite

时间丢失
→ temporal_loss
```

### 38.3 Access Failure Fixtures

```text
错误 query
→ query_failure

没用 temporal
→ strategy_failure

错误 filter
→ filter_failure

正确 Memory 在 Top-k 外
→ ranking_failure

Tool 返回但 workspace 丢失
→ context_failure

必要 Memory 在最终 Answer Prompt 构建前被选择器移除
→ selection_context_failure

多跳 Memory 仅部分进入最终 Answer Prompt
→ context_coverage_or_conflict
```

### 38.4 Other/Invalid

```text
最终 Answer Prompt 已含充分 Memory，但弱模型答案错误
→ other / model_failure

S1 判为 FULL，但强模型从相同上下文也答错
→ review_required / semantic_judge_inconsistent

Reference 不受 source 支持
→ invalid
```

### 38.5 Future Leakage

创建 snapshot 后的新 Memory Version：

- 可以出现在 audit lineage；
- `available_at_snapshot=false`；
- 不得进入 Memory Coverage；
- 不得进入 Oracle Access。

### 38.6 Mixed Failure

删除正确 Memory，同时给 Access 错误策略：

1. Primary 为 Construction；
2. Oracle Construction 后仍失败；
3. Access 进入 contributor；
4. Learning Router 先返回 Construction。

---

## 39. Failure Agent 评测

构造受控 corruption：

```text
drop_memory
corrupt_time
wrong_overwrite
hide_retrieval_hit
wrong_filter
evict_context
remove_final_evidence
force_wrong_answer
```

因为 corruption 类型已知，可以评估：

- primary label accuracy；
- subtype accuracy；
- first broken edge accuracy；
- provenance citation accuracy；
- counterfactual consistency；
- auto-learn routing precision；
- engineering issue routing precision。

仅用自然失败无法获得可靠的归因 ground truth，因此受控 corruption 是必要消融。

---

## 40. 实施顺序

### Phase A：Provenance 写入

1. Message stable ID；
2. construction_inputs；
3. Candidate/Message edge；
4. Decision；
5. Version direct source；
6. Parent edge；
7. lineage closure；
8. change diff。

验收：给任意 Message，能查到完整 Memory descendants。

### Phase B：Access Trace

1. access_runs；
2. access_actions；
3. retrieval_hits；
4. workspace flag；
5. `access_answer_context`；
6. Answer Prompt hash；
7. final evidence。

验收：给任意 Memory Version，能查到是否被召回、是否真正进入最终 Answer Prompt、是否被选用；Bundle 中的 Prompt hash 与 Runtime trace 一致。

### Phase C：Algorithmic Failure

1. FailureCase；
2. ProvenanceGraph；
3. algorithmic Coverage；
4. First Break；
5.基本 subtype。

验收：不调用 LLM 也能定位结构性断点。

### Phase D：Semantic FailureAgent

1. 隔离的 Runtime Context sufficiency；
2. 同上下文强模型重答；
3. Gold Evidence Unit；
4. raw support；
5. snapshot memory support；
6. confidence。

验收：充分 Memory 已进入最终 Answer Prompt 时能优先判为 `model_failure`，且 S1 不能看到 Raw Message。

### Phase E：Counterfactual 与 Router

1. Oracle Retrieval；
2. Oracle Construction；
3. Oracle Access；
4. contributor；
5. Skill/Engineering route。

验收：Mixed Failure 能顺序修复。

---

## 41. 最终验收

- [ ] 每个 LoCoMo Message 有稳定 ID；
- [ ] 每个 Candidate 引用合法 Message；
- [ ] 每个 Decision 可查；
- [ ] SKIP 也可追溯；
- [ ] 每个 Memory Version 有 direct source；
- [ ] UPDATE/MERGE 有 Parent edge；
- [ ] Lineage closure 由纯算法生成；
- [ ] Change diff 由纯算法生成；
- [ ] Access hit、workspace、final answer-visible context 和 final evidence 分开记录；
- [ ] Answer Prompt hash 可校验，Failure 第一步读取精确可见 Memory；
- [ ] S1 输入不包含 Raw Message 或 Store 中未展示的 Memory；
- [ ] 可见 Memory 充分但弱模型答错时归为 `other/model_failure`；
- [ ] Failure Agent 使用只读 Store；
- [ ] Future Version 不进入 snapshot coverage；
- [ ] Coverage Matrix 能定位第一个断点；
- [ ] Construction/Access/Other/Invalid 分类明确；
- [ ] Engineering Issue 不进入 Skill Bank；
- [ ] Oracle Replay 不污染真实 Store；
- [ ] Mixed Failure 使用 primary + contributor；
- [ ] 低置信度报告不自动学习；
- [ ] Failure Report 中所有 ID 可反查；
- [ ] 受控 corruption 测试通过。

---

## 42. 最终判断

这套 Failure 架构的关键不是让强模型“更聪明地读日志”，而是先建立可靠的数据血缘：

```text
稳定 Message ID
→ Candidate 来源
→ Construction Decision
→ Memory Version Parent
→ Message Lineage Closure
→ Access Retrieval Path
→ Final Evidence
```

在此基础上：

```text
算法定位执行断点
强模型判断语义充分性
Counterfactual 验证因果
Router 决定 Skill 还是工程修复
```

它具备：

- 工程可实现性；
- 只读安全边界；
- 版本级溯源；
- 多跳证据支持；
- 混合错误处理；
- 防止未来数据泄漏；
- 防止 Skill Bank 被工程 Bug 污染；
- 可用受控 corruption 定量评测。

第一版不需要图数据库。SQLite 关系表与 materialized lineage closure 已足够支撑 LoCoMo 和当前 Demo。

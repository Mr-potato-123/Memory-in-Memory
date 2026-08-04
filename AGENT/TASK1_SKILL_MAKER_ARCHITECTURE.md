# MiM Skill-Maker Architecture

## 面向 Access / Construction Failure 的最小可用 Skill 迭代系统

> 文档定位：独立的维护侧 Skill-Maker 实现设计。  
> 上游输入：`FailureReport`。  
> 下游产物：新的 Skill Candidate、Skill Bank Version 和 Replay Report。  
> 当前优先级：先实现 Access Failure，再实现 Construction Failure。

---

## 1. 目标与边界

Skill-Maker 只做一件事：

> 把已经定位清楚的 Failure 转换成一条可复用的自然语言 Skill，并通过 Replay 验证它真的能够被运行侧检索和使用。

它不负责：

- 重新诊断 Failure 类型；
- 修改事实 Memory；
- 修改 Retrieval 算法、top-k、embedding 或 RRF 权重；
- 修改 Agent 工具权限；
- 修改模型；
- 在 validation/test 数据上学习；
- 为 `other/model_failure`、`invalid` 或工程 Bug 创建 Skill。

允许进入 Skill-Maker 的 Failure：

```text
access
construction
```

不允许进入：

```text
other
invalid
index_failure
persistence_failure
trace_missing
schema_or_protocol_error
review_required=true
```

---

## 2. 最小 Skill 格式

Skill 对模型可见的正文严格只有三个字段：

```json
{
  "name": "Retrieve all states needed for a temporal question",
  "description": "Use when a question asks about a past or current state and the same entity may have changed over time.",
  "content": "Identify the target time before searching. Use temporal retrieval and include history when the question asks about a past state. Do not answer until the memory valid at the target time is visible."
}
```

三个字段职责必须分开：

| 字段 | 职责 | 主要使用者 |
| --- | --- | --- |
| `name` | 简短标识，方便日志和人工阅读 | 人、日志 |
| `description` | 描述“什么时候应该调用这条 Skill” | Skill Retriever |
| `content` | 描述“检索到以后具体应该怎么做” | Runtime Agent |

最重要的约束：

```text
description 决定能否被检索
content 决定检索后能否修复
```

因此：

- Candidate 没被检索到，优先只修改 `description`；
- Candidate 已被检索但没有修复，优先修改 `content`；
- `name` 默认不参与修复循环，只在明显误导时修改。

### 2.1 系统元数据与 Skill 正文分离

为了 CRUD、版本化和溯源，系统必须保存元数据，但元数据不属于 Skill 的三字段格式：

```json
{
  "skill_id": "sk_access_00017",
  "version": 3,
  "side": "access",
  "status": "staged",
  "payload": {
    "name": "...",
    "description": "...",
    "content": "..."
  },
  "parent_version_id": "sk_access_00017_v2",
  "created_from_failure_id": "failure_conv2_qa14",
  "bank_version_created": null,
  "created_at": "2026-07-27T10:00:00Z"
}
```

运行时只注入 `payload`。Skill-Maker、Repository 和审计系统使用外层元数据。

---

## 3. 核心设计原则

### 3.1 每个 Failure 都先生成一个新 Draft

每个进入系统的失败案例，都先独立生成一个新的三字段 Draft：

```text
FailureReport
→ New Skill Draft
```

这里的“新 Skill”首先表示新的 Candidate，不表示它一定成为一条新的 Active Skill。

Draft 生成后再读取现有 Skill Bank，决定：

```text
CREATE
UPDATE
REUSE
DELETE/TOMBSTONE
```

这样既满足“每个问题都产生一个新 Skill 候选”，又避免同一种失败在 Bank 中堆积大量重复 Skill。

### 3.2 Draft 阶段先不看旧 Skill

生成 Draft 时只提供：

- Failure 类型；
- subtype；
- first broken edge；
- 失败轨迹中的必要片段；
- 当前 Agent 的工具契约；
- Runtime 允许的行为。

不提供旧 Skill，避免模型只是改写已有表述。

Draft 生成以后，才检索相似 Skill 并执行 CRUD 决策。

如果训练是串行的，某条 Failure 从产生到被处理之间，Active Bank 可能已经加入了能够修复它的新 Skill。因此 Draft 之后先用当前 Active Bank 做一次自然 Replay：

```text
当前 Active Bank 已自然修复
→ REUSE

当前 Active Bank 仍失败
→ 继续 CREATE / UPDATE
```

`REUSE` 不能只根据名称相似度或强模型判断产生。

为避免把模型随机波动误判成复用，REUSE 还要求：

- Replay 使用与原失败相同的模型、Prompt、预算和确定性参数；
- trace 中实际检索到至少一条原失败 Bank Version 之后新增或更新的 Skill；
- 记录这些实际使用的 Skill Version IDs。

### 3.3 Candidate 未验证前不能进入 Active Bank

Candidate 先进入：

```text
Staging Skill Bank
```

只有通过全部 Gate 后，才原子发布到：

```text
Active Skill Bank
```

不能先把未经验证的 Skill 加入正式 Bank，再等待后续失败来修正。

### 3.4 Skill 上线前必须能被自然检索

只证明“强制注入后有效”还不够。

一条 Skill 的上线条件必须同时满足：

```text
Forced Replay 修复目标
AND
Natural Retrieval 能检索到 Candidate
AND
Natural Replay 修复目标
AND
最小回归通过
```

---

## 4. Skill 生命周期

```text
DRAFT
→ STAGED
→ RETRIEVAL_TESTING
→ REPLAY_TESTING
→ ACCEPTED
→ ACTIVE
```

失败分支：

```text
not_retrieved
→ revise description
→ RETRIEVAL_TESTING

retrieved_but_failed
→ revise content
→ Forced Replay

regression_failed
→ revise / reject

attempts_exhausted
→ REJECTED

protocol_error
→ ENGINEERING_ISSUE
```

允许状态：

```text
draft
staged
active
rejected
tombstoned
```

---

## 5. CRUD 定义

### 5.1 CREATE

适用条件：

- 没有语义和用途相近的 Active Skill；
- 新 Failure 暴露了新的触发条件或策略；
- Candidate 通过所有 Replay Gate。

结果：

- 创建新的 `skill_id`；
- 发布新的 Skill Bank Version；
- Candidate 状态变为 `active`。

### 5.2 READ

READ 包括：

- 按 `skill_id/version` 读取；
- 按 side 列出 Active Skill；
- 用 Runtime 的真实 Skill Retriever 检索；
- 读取历史版本；
- 查某条 Skill 来自哪些 Failure；
- 查某次 Replay 使用了哪些 Skill。

Skill-Maker 不得使用一套“专门更容易命中”的检索逻辑。检索测试必须调用与 Runtime 完全相同的 Retriever。

### 5.3 UPDATE

适用条件：

- 已有 Skill 的适用场景与当前 Failure 相同；
- 但 description 不能稳定召回；
- 或 content 对该类 Failure 不够完整。

UPDATE 不覆盖旧版本：

```text
skill_017_v1
→ skill_017_v2
→ skill_017_v3
```

每次修改必须记录：

```text
changed_fields
change_reason
source_failure_id
replay_result
parent_version_id
```

### 5.4 REUSE

如果已有 Active Skill：

- 能自然检索到；
- 能修复当前 Failure；
- 不需要修改；

则不创建新的 Active Skill，记录：

```text
crud_action = REUSE
reused_skill_id = ...
```

当前 Failure 生成的 Draft 保存在 Candidate 审计目录，状态为：

```text
rejected_as_duplicate
```

### 5.5 DELETE

MVP 不执行物理删除，只执行 Tombstone。

适用条件：

- 完全重复；
- 被更通用的新版本取代；
- 持续造成回归；
- 包含具体答案、人物或数据泄漏；
- 内容违反工具权限。

```text
status = tombstoned
```

旧版本继续保留用于实验复现，但不再进入 Runtime Skill Index。

单个 Failure 不得直接删除一条已有 Active Skill。自动 Tombstone 至少需要：

- 明确的安全/泄漏违规；或
- 多次回归证据；或
- 人工确认。

---

## 6. 总体工作流

```text
FailureReport
→ Route Guard
→ Generate New Draft
→ Validate Skill Payload
→ Search Similar Active Skills
→ Current-bank Replay
   ├─ Fixed → REUSE
   └─ Still Failed → Decide CREATE / UPDATE
→ Forced Replay
→ Natural Retrieval Gate
→ Natural Replay
→ Minimal Regression
→ Publish New Skill Bank Version
```

受控状态机：

```text
S0 LOAD_FAILURE
→ S1 DRAFT_NEW_SKILL
→ S2 REUSE_PROBE_AND_CRUD_PLAN
   ├─ Current Bank Fixed → S9 RECORD_REUSE
   └─ Still Failed → CREATE / UPDATE Candidate
→ S3 FORCE_VALIDATE_CONTENT
→ S4 VALIDATE_NATURAL_RETRIEVAL
   ├─ Not Retrieved → S5 REVISE_DESCRIPTION → S4
   └─ Retrieved → S6 NATURAL_REPLAY
       ├─ Fixed → S7 REGRESSION
       └─ Not Fixed → S8 REVISE_CONTENT → S3
→ S9 PUBLISH_OR_REJECT
```

所有状态跳转由 Python Workflow 决定。强模型只负责：

- 生成 Draft；
- 修改 description；
- 修改 content；
- 在给定候选之间提出 CRUD 建议。

强模型不能直接写 Skill Bank。

---

## 7. Access Failure：优先实现

Access Skill-Maker 使用 Failure Agent 已经确认的前提：

```text
Raw source 支持答案
Snapshot Store 中存在充分 Memory
Runtime 最终 Answer Prompt 中的 Memory 不充分
```

典型 subtype：

```text
query_failure
strategy_failure
filter_failure
ranking-related strategy failure
context_coverage_or_conflict
premature_stop
```

工程 Bug，例如索引损坏或 workspace 代码漏写，不进入 Skill-Maker。

### 7.1 Access Draft 输入

只输入修复 Access 策略所需信息：

```text
question
failure subtype
first broken edge
actual Access actions
actual queries/filters
missing evidence characteristics
available tools and parameters
budget/max steps
Failure Agent recommendation
```

可以提供必要的 Memory 类型和时间关系，但 Skill 输出不得包含：

- 人名；
- 地名；
- Message ID；
- Memory ID；
- QA 标准答案；
- 当前数据集专有措辞。

### 7.2 Access Forced Replay

目的：

> 当 Runtime Agent 一定能看到 Candidate Skill 时，content 是否能够修复原失败？

固定：

- 原失败的 `snapshot_commit_id`；
- 相同 Runtime 模型；
- 相同 Prompt；
- 相同工具；
- 相同检索配置；
- 相同预算；
- 相同 seed/temperature；
- 原 Active Skills；
- 强制额外注入 Candidate。

Access Replay 不写 Memory。

结果：

```text
forced_passed
forced_failed
protocol_error
```

如果 `forced_failed`：

- Candidate 即使能被检索也没有价值；
- 修改 `content`；
- 重新 Forced Replay；
- 不要先修改 description。

### 7.3 Natural Retrieval Gate

Forced Replay 通过后，把 Candidate 加入临时 Staging Index，使用 Runtime 的真实查询构造器和真实 Skill Retriever：

```python
query = runtime_skill_query_builder.for_access(
    question=case.question,
    access_state=case.initial_access_state,
)

hits = runtime_skill_retriever.search(
    query=query,
    side="access",
    bank=staging_bank,
    top_k=config.skill_top_k,
)
```

必须验证：

```text
candidate_version_id in hits
```

不能只验证 cosine score 超过某个离线阈值，因为真正 Runtime 使用的是 top-k 竞争。

### 7.4 未检索到时如何修改

当 Candidate 不在 top-k 中：

```text
只修改 description
content 保持不变
```

Description Reviser 输入：

- 当前 description；
- Runtime skill query；
- Candidate 当前 rank/score；
- top-k Skill 的 name/description；
- Failure subtype；
- 禁止泄漏规则。

输出仍然只有新的：

```json
{
  "description": "..."
}
```

修改目标是更准确表达可泛化的触发条件，而不是复制原问题。

错误做法：

```text
Use this when Alice asks where Bob lived in 2019.
```

正确方向：

```text
Use when a question asks for an entity's state at a specific past time and memory may contain both current and historical states.
```

每次修改后：

1. 创建新的 Candidate Version；
2. 重建 Staging Index；
3. 用同一个 Runtime query 重新检索；
4. 记录 rank、score 和 top-k；
5. 直到命中或次数用尽。

### 7.5 Natural Replay

Candidate 被自然检索到以后，不再强制注入，完整运行一次 Access & Answer：

```text
Staging Bank
→ normal Skill Retrieval
→ normal Access loop
→ final answer
```

结果：

| 结果 | 下一步 |
| --- | --- |
| Candidate 未检索到 | 修改 description |
| 已检索且答案修复 | 进入回归 |
| 已检索但仍失败 | 修改 content，返回 Forced Replay |
| Protocol/Tool Error | 路由工程问题，不消耗 Skill 修改预算 |

这一区分必须依赖 trace 中的实际 `skill_version_ids`，不能让强模型凭感觉判断是否检索到。

---

## 8. Construction Failure：只修第一个断点

Construction Skill-Maker 不尝试一次修复整条 Construction 链。

Failure Agent 必须提供：

```text
primary_label = construction
first_broken_edge
primary_subtype
related_message_ids
candidate_ids
decision_ids
memory_version_ids
snapshot_commit_id
```

Skill-Maker 在一次修复循环中冻结：

```text
target_first_break
```

即使 Replay 暴露下游新问题，也不能在同一 Candidate 中顺便加入第二套规则。

### 8.1 第一个断点与成功条件

| First Break | 本轮只修什么 | 本地成功条件 |
| --- | --- | --- |
| `message_to_construction` | 输入遗漏 | Gold Message 被纳入 Construction input |
| `message_to_candidate` | 抽取遗漏 | 生成包含目标 claim 的 Candidate |
| `candidate_to_decision` | 决策错误 | Candidate 得到正确 ADD/UPDATE/MERGE/SKIP |
| `decision_to_version` | 表达损失 | 生成语义和时间上充分的 Memory Version |
| `version_to_version` | 更新/合并损失 | Child Version 保留应保留的事实与来源 |

其中：

- 结构状态由算法判断；
- Candidate/Memory 是否语义充分由 Failure 强模型判断；
- 不使用最终答案是否正确代替第一个断点的本地判定。

### 8.2 Construction 迭代循环

```text
固定 first broken edge
→ 生成/修改 Candidate content
→ 从原始 Message 重放 Construction
→ 检查同一个 first broken edge
   ├─ 仍未修复 → 修改 content，继续
   └─ 已修复 → Natural Retrieval Gate
→ 自然 Construction Replay
→ 再检查同一个 first broken edge
→ 发布或拒绝
```

达到最大尝试次数仍未修复：

```text
status = rejected
reason = attempts_exhausted
Active Bank 不变
```

### 8.3 为什么不同时修下游

例如：

```text
Message 未生成 Candidate
```

本轮 Skill 只应指导“何种信息必须被抽取”。

如果修复后 Candidate 已生成，但新的 Replay 中出现：

```text
Candidate 被错误 SKIP
```

正确处理是：

1. 当前第一个断点已修复；
2. 保存本轮结果；
3. 重新运行 Failure Agent；
4. 得到新的 `first_broken_edge=candidate_to_decision`；
5. 启动下一轮独立 Skill Candidate。

不能把抽取、决策、时间更新、Access 查询全部写进一条 Skill。

### 8.4 Construction Replay

Construction 会改变 Memory，不能直接修改失败时的 final Store。

MVP 使用最稳妥的方式：

```text
创建临时 Store
→ 从该 conversation 的 Raw Message 重新构建
→ 在目标 Construction 阶段强制或自然注入 Candidate
→ 生成新的 Candidate/Decision/Memory Version
→ 检查冻结的 first broken edge
```

通过本地 first-break 检查后，再运行目标 QA 作为观察项：

- QA 修复：当前 Failure 完成；
- QA 仍错但 first break 已修复：重新交给 Failure Agent 定位新断点；
- first break 未修复：继续修改当前 Candidate；
- 不允许为了下游错误扩大当前 Skill content。

Construction Candidate 同样必须通过 Natural Retrieval Gate。未被对应 Construction 阶段检索到时，只修改 description。

---

## 9. 尝试次数与终止条件

建议 MVP 配置：

```yaml
skill_maker:
  max_total_revisions: 6
  max_description_revisions: 3
  max_content_revisions: 3
  skill_top_k: 3
  regression_buffer_size: 10
  infrastructure_retry: 1
```

规则：

- 每次 LLM 修改 description 或 content，消耗一次 revision；
- 单纯读取、检索和确定性评分不消耗 revision；
- Protocol/基础设施错误先重试一次，不应诱导模型修改 Skill；
- 任一预算耗尽，Candidate 进入 `rejected`；
- 拒绝不会改变 Active Bank；
- 所有尝试、Prompt hash、模型和结果必须保存。

终止状态：

```text
accepted_create
accepted_update
reused_existing
rejected_attempts_exhausted
rejected_regression
rejected_invalid_payload
engineering_issue
record_only
```

---

## 10. Skill Payload 校验

在任何 Replay 前执行确定性校验：

```python
class SkillPayload(BaseModel):
    name: str
    description: str
    content: str
```

基本限制：

```yaml
name_max_chars: 80
description_max_chars: 320
content_max_chars: 1200
```

校验：

- 三个字段非空；
- 不允许额外字段；
- 不包含 reference answer；
- 不包含 Message/Memory/Candidate ID；
- 不包含当前 case 的专有人物、地点等实体；
- 不要求 Runtime 不具备的工具；
- 不改变 schema、top-k、预算或系统配置；
- content 必须是可执行策略，不是案例复述；
- description 必须描述触发条件。

实体泄漏检查可以先使用：

- Gold source 中的实体字符串匹配；
- ID 正则；
- reference answer 归一化匹配；
- question 中专有实体匹配。

MVP 不需要额外 LLM 做安全检查。

---

## 11. CRUD 决策规则

Draft 完成后检索同侧相似 Active Skill：

```text
side 必须相同
access 不能更新 construction
construction 不能更新 access
```

最小决策：

```python
current_replay = replay.run_with_active_bank(case)

if current_replay.fixed:
    action = REUSE
elif no_similar_skill:
    action = CREATE
elif same_trigger_and_same_goal:
    action = UPDATE
else:
    action = CREATE
```

`same_trigger_and_same_goal` 可由强模型判断，但输入只给：

- Draft；
- top-N 同侧 Skill；
- Failure signature；
- 每条 Skill 在当前 case 的检索与 Replay 结果。

最终 CRUD 操作由 Workflow 执行。

为保持 MVP 简单：

- 每次最多 UPDATE 一条已有 Skill；
- 不做多 Skill 自动 Merge；
- 多条重复 Skill 先选择最相近的一条 UPDATE；
- 其余重复项只生成清理建议，不自动 Tombstone。

---

## 12. 最小回归

一条 Candidate 修复当前 Failure 仍可能破坏旧案例。

维护一个最多 10 条的 train-only Replay Buffer：

```text
最近修复的 5 条同侧 Failure
+
与 Candidate 最相似的 5 条已修复 Failure
```

验收：

```text
当前目标必须修复
AND
历史已通过案例不得从 pass 变为 fail
```

Access 回归使用各自冻结的 Memory Snapshot。

Construction 回归从各自 Raw Conversation 重建，不共享事实 Memory。

Validation/test 不得进入：

- Draft；
- Description 修改；
- Content 修改；
- CRUD 决策；
- Replay Buffer。

---

## 13. Bank 发布与版本

Skill Bank 更新串行执行：

```text
bank_v000
→ bank_v001
→ bank_v002
```

发布事务：

1. 读取当前 Active Bank Version；
2. 校验 Candidate 的 parent version；
3. 执行 CREATE/UPDATE/TOMBSTONE；
4. 构建新的 Skill Index；
5. 运行最终 Natural Retrieval smoke test；
6. 原子保存新 Bank；
7. 将 `selected/latest` 指针切换到新版本。

如果第 4～6 步失败：

```text
回滚发布
旧 Active Bank 保持不变
Candidate 保留 staged/rejected 状态
```

训练阶段可使用 `latest`。

Validation 结束后选择：

```text
selected.json
```

Test 只读 `selected.json`。

---

## 14. 数据接口

### 14.1 Failure 输入

```python
class SkillMakerInput(BaseModel):
    failure_report: FailureReport
    failure_bundle_ref: str
    active_bank_version: str
    replay_config: ReplayConfig
```

### 14.2 Candidate

```python
class SkillCandidate(BaseModel):
    candidate_id: str
    skill_id: str
    version: int
    side: Literal["access", "construction"]
    payload: SkillPayload
    source_failure_id: str
    target_first_break: str | None
    parent_version_id: str | None
    status: Literal["draft", "staged", "accepted", "rejected"]
```

### 14.3 Attempt

```python
class SkillAttempt(BaseModel):
    attempt_id: str
    candidate_version_id: str
    revision_kind: Literal[
        "initial",
        "description",
        "content",
    ]
    retrieval_rank: int | None
    retrieved: bool | None
    forced_replay_passed: bool | None
    natural_replay_passed: bool | None
    first_break_repaired: bool | None
    regression_passed: bool | None
    prompt_hash: str
    model_id: str
```

### 14.4 输出

```python
class SkillMakerResult(BaseModel):
    failure_id: str
    action: Literal["CREATE", "UPDATE", "REUSE", "NONE"]
    status: str
    accepted_skill_version_id: str | None
    new_bank_version: str | None
    attempts: list[SkillAttempt]
    final_reason: str
```

---

## 15. Repository 接口

```python
class SkillRepository(Protocol):
    def get(self, skill_id: str, version: int | None = None) -> SkillRecord:
        ...

    def list_active(self, side: str, bank_version: str) -> list[SkillRecord]:
        ...

    def stage_create(self, candidate: SkillCandidate) -> SkillRecord:
        ...

    def stage_update(
        self,
        skill_id: str,
        candidate: SkillCandidate,
    ) -> SkillRecord:
        ...

    def tombstone(self, skill_id: str, reason: str) -> SkillRecord:
        ...

    def publish(
        self,
        staged_record: SkillRecord,
        parent_bank_version: str,
    ) -> str:
        ...
```

`delete` 对外表现为 `tombstone`，不暴露硬删除。

---

## 16. Replay 接口

```python
class SkillReplayRunner(Protocol):
    def forced_access_replay(
        self,
        case: FailureCase,
        candidate: SkillCandidate,
    ) -> ReplayResult:
        ...

    def natural_access_replay(
        self,
        case: FailureCase,
        staging_bank: SkillBank,
    ) -> ReplayResult:
        ...

    def forced_construction_replay(
        self,
        case: FailureCase,
        candidate: SkillCandidate,
        target_first_break: str,
    ) -> ReplayResult:
        ...

    def natural_construction_replay(
        self,
        case: FailureCase,
        staging_bank: SkillBank,
        target_first_break: str,
    ) -> ReplayResult:
        ...
```

所有 Replay 返回：

- 实际检索到的 Skill Version IDs；
- Candidate rank 和 score；
- Runtime trace；
- Answer；
- Answer metric；
- Construction first-break 本地检查；
- Store/Prompt/config/model hash。

---

## 17. Access Workflow 伪代码

```python
def repair_access_failure(case, report, active_bank):
    guard.assert_learnable_access(report)

    draft = maker.draft_access(report)
    validator.validate(draft, case)

    current_replay = replay.natural_access_replay(
        case=case,
        bank=active_bank,
    )
    if (
        current_replay.answer_correct
        and current_replay.used_new_or_updated_skill_ids
    ):
        return record_reuse(
            failure_id=report.failure_id,
            actual_skill_version_ids=current_replay.skill_version_ids,
        )

    plan = crud_planner.plan(
        draft=draft,
        similar_skills=skill_retriever.search_similar_payloads(
            draft, side="access"
        ),
    )
    candidate = repository.stage(plan, draft)

    while budget.remaining:
        forced = replay.forced_access_replay(case, candidate)
        if forced.is_protocol_error:
            return handle_engineering_error(forced)

        if not forced.answer_correct:
            candidate = maker.revise_content(
                candidate=candidate,
                failure_report=report,
                replay=forced,
            )
            validator.validate(candidate.payload, case)
            budget.consume("content")
            continue

        retrieval = retrieval_gate.run(
            case=case,
            candidate=candidate,
            staging_bank=repository.build_staging_bank(candidate),
        )
        if not retrieval.candidate_retrieved:
            candidate = maker.revise_description(
                candidate=candidate,
                retrieval_trace=retrieval,
            )
            validator.validate(candidate.payload, case)
            budget.consume("description")
            continue

        natural = replay.natural_access_replay(
            case,
            repository.build_staging_bank(candidate),
        )
        if not natural.candidate_retrieved:
            candidate = maker.revise_description(candidate, natural)
            budget.consume("description")
            continue

        if not natural.answer_correct:
            candidate = maker.revise_content(candidate, report, natural)
            budget.consume("content")
            continue

        if not regression.run(candidate):
            return reject(candidate, "regression_failed")

        return repository.publish(candidate, active_bank.version)

    return reject(candidate, "attempts_exhausted")
```

---

## 18. Construction Workflow 伪代码

```python
def repair_construction_failure(case, report, active_bank):
    guard.assert_learnable_construction(report)
    target = report.first_broken_edge

    draft = maker.draft_construction(
        report=report,
        target_first_break=target,
    )
    validator.validate(draft, case)

    current_replay = replay.natural_construction_replay(
        case=case,
        bank=active_bank,
        target_first_break=target,
    )
    if (
        current_replay.first_break_repaired
        and current_replay.used_new_or_updated_skill_ids
    ):
        return record_reuse(
            failure_id=report.failure_id,
            actual_skill_version_ids=current_replay.skill_version_ids,
        )

    candidate = repository.stage_new_draft(draft)

    while budget.remaining:
        forced = replay.forced_construction_replay(
            case=case,
            candidate=candidate,
            target_first_break=target,
        )
        if forced.is_protocol_error:
            return handle_engineering_error(forced)

        if not forced.first_break_repaired:
            candidate = maker.revise_content(
                candidate=candidate,
                failure_report=report,
                replay=forced,
                immutable_target=target,
            )
            budget.consume("content")
            continue

        retrieval = retrieval_gate.run_construction(
            case=case,
            candidate=candidate,
            target_stage=target,
        )
        if not retrieval.candidate_retrieved:
            candidate = maker.revise_description(candidate, retrieval)
            budget.consume("description")
            continue

        natural = replay.natural_construction_replay(
            case=case,
            staging_bank=repository.build_staging_bank(candidate),
            target_first_break=target,
        )
        if not natural.first_break_repaired:
            candidate = maker.revise_content(
                candidate,
                report,
                natural,
                immutable_target=target,
            )
            budget.consume("content")
            continue

        if not regression.run(candidate):
            return reject(candidate, "regression_failed")

        published = repository.publish(candidate, active_bank.version)

        if not natural.answer_correct:
            failure_queue.enqueue(
                failure_agent.analyze(natural.replayed_case)
            )

        return published

    return reject(candidate, "attempts_exhausted")
```

---

## 19. 建议项目结构

```text
src/mim/
├─ agents/
│  └─ skill_maker.py
├─ skill_maker/
│  ├─ models.py
│  ├─ workflow.py
│  ├─ crud_planner.py
│  ├─ validator.py
│  ├─ retrieval_gate.py
│  ├─ replay.py
│  ├─ regression.py
│  └─ repository.py
├─ skills/
│  ├─ retriever.py
│  ├─ query_builder.py
│  └─ bank.py
└─ schemas/
   ├─ failure.py
   └─ skill.py

outputs/<run_id>/skills/
├─ banks/
│  ├─ bank_v000.json
│  ├─ bank_v001.json
│  └─ selected.json
├─ candidates/
│  └─ <candidate_id>/
│     ├─ versions.jsonl
│     ├─ retrieval_trials.jsonl
│     ├─ replay_trials.jsonl
│     └─ result.json
└─ index/
   └─ <bank_version>.npz
```

MVP 可以全部使用 JSON/JSONL + NumPy，不需要额外数据库或向量数据库。

---

## 20. 最小实现顺序

### Phase A：Skill Bank 与三字段 Schema

实现：

- `SkillPayload`；
- 系统元数据；
- immutable version；
- list/get/create/update/tombstone；
- Access/Construction side 隔离。

### Phase B：Access Skill-Maker

实现：

- Access Draft；
- Forced Replay；
- Natural Retrieval Gate；
- description-only revision；
- Natural Replay；
- publish/reject。

这是当前第一优先级。

### Phase C：Construction First-Break Repair

实现：

- 冻结 `first_broken_edge`；
- Construction 临时 Store Replay；
- first-break 本地判定；
- content revision；
- Natural Retrieval Gate；
- 修复后重新进入 Failure Queue。

### Phase D：最小回归与 CRUD 完善

实现：

- train-only buffer；
- REUSE；
- UPDATE；
- Tombstone；
- Bank Version selection。

---

## 21. 验收条件

- [ ] Skill 正文只有 `name/description/content`；
- [ ] 每个可学习 Failure 都生成一个新 Draft；
- [ ] Access 与 Construction Skill 分开检索；
- [ ] Candidate 未验证前不进入 Active Bank；
- [ ] Forced Replay 失败时修改 content；
- [ ] Natural Retrieval 未命中时只修改 description；
- [ ] 检索验证使用 Runtime 的真实 query builder、Retriever 和 top-k；
- [ ] Skill 实际出现在 Runtime trace 中才算 retrieved；
- [ ] 达到次数上限后拒绝 Candidate，Active Bank 不变；
- [ ] Construction 循环冻结并只修第一个错误点；
- [ ] Construction 第一个断点修复后，剩余错误重新交给 Failure Agent；
- [ ] CREATE/UPDATE 生成不可变新版本；
- [ ] DELETE 使用 Tombstone；
- [ ] Skill 不包含当前问题的实体、ID 或标准答案；
- [ ] `other/invalid/engineering_issue` 不进入 Skill-Maker；
- [ ] Validation/Test 不更新 Skill Bank；
- [ ] 所有 Candidate Version、Retrieval Trial 和 Replay Trial 可追溯。

---

## 22. 最终简化逻辑

Access Failure：

```text
生成 Skill
→ 强制注入是否有效？
   ├─ 否：改 content
   └─ 是：自然检索能否找到？
       ├─ 否：改 description
       └─ 是：自然 Replay 是否修复？
           ├─ 否：改 content
           └─ 是：回归并上线
→ 直到成功或次数用尽
```

Construction Failure：

```text
锁定第一个错误点
→ 生成 Skill
→ 重放 Construction
→ 第一个错误点修好了吗？
   ├─ 否：只围绕该点改 content
   └─ 是：验证自然检索与回归
→ 上线
→ 若仍有后续错误，重新交给 Failure Agent 定位
→ 直到当前点修复或次数用尽
```

这两个循环共享同一套 Skill Schema、CRUD、Retrieval Gate、版本和审计机制；差别只在 Replay 环境和成功判定。

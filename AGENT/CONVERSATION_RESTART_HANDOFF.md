# MiM 对话重新启用：项目与协作交接说明

> 面向新接手本项目的 Agent / 代码协作者。阅读本文件后，应能直接理解项目目标、当前架构、用户的关键立场、已完成工作，以及后续应如何与用户协作。

## 0. 当前状态（一句话）

项目已完成从旧式玩具 Demo 到**新版最小可用评测架构**的收敛：一个 SQLite 事实记忆真相源、四个 Agent、三个运行工作流、可追溯的 Failure 诊断、三字段 Skill 及其 CRUD/重放验证。旧 JSON Memory 与旧 replay 双路径已经移除，**不要重新引入兼容层**。

代码根目录：

```text
D:\Documents\Project\Memory_in_Memory\single_agent_mim
```

当前验证状态（2026-07-28）：

```text
python -m pytest -q     # 22 passed
python main.py smoke    # passed
```

上述验证使用 MockClient 和确定性 embedding；真实 GPT/Qwen/Claude 端点已具备适配接口，但尚未用用户密钥做完整训练实验。

## 1. 用户想做什么

项目名为 **Memory in Memory（MiM）**。它不是要做一个臃肿的通用 Agent 平台，也不是要重新造一个记忆基座。它要在一个单 Agent 记忆系统上实现一个可评测的“错误驱动策略插件”：

1. Runtime 先构建和访问长期记忆；
2. QA 失败后，Maintenance 诊断失败究竟发生在什么位置；
3. 将可泛化的修复策略抽象成 Skill；
4. 后续 Runtime 检索并使用 Skill，改善类似错误；
5. 所有记忆版本、改写和答案依据都能回溯到原始对话消息。

用户明确希望它是**最小可用版本（MVP）**，但不是玩具：基础接口、存储边界、Agent 职责、workflow、训练/使用/评测方式必须清晰，可由代码人员继续维护。

## 2. 用户的关键观点与不可违背的设计决定

以下是多轮讨论后已经确认的立场。后续设计如果与它们冲突，应先明确告知用户并讨论，而不是默默改方向。

### 2.1 四个 Agent，且 Access 与 Answer 合并

最终的四个 Agent 是：

| 侧 | Agent | 作用 |
|---|---|---|
| Runtime（弱模型） | Construction Agent | 从会话构建版本化 Memory |
| Runtime（弱模型） | Access & Answer Agent | 可边检索边回答；不是两个独立 Agent |
| Maintenance（强模型） | Failure Agent | 判断失败原因、做证据验证和溯源编排 |
| Maintenance（强模型） | Skill-Maker Agent | 生成/修改 Skill，并经验证后发布 |

用户明确倾向于把 Access 和 Answer 合并，因为实际问答应能“边 access 边答”。不要重新拆出单独的 Answer Agent。

### 2.2 最小，但每层必须有清晰责任

允许暂时不做的内容：分布式服务、复杂向量数据库、Web UI、多租户、异步队列、生产级并发调度。

不能省略的内容：

- `use / train / evaluate` 的明确接口；
- 版本化 Memory；
- 原始消息来源与版本改写来源；
- Failure 的清晰工作流和明确返回；
- Skill 的 CRUD、自然检索闸门和回放验证；
- 冻结的 validation/test 语义；
- 可读的项目树和交接文档。

### 2.3 Memory 必须可溯源，且版本改写也必须可溯源

用户特别强调：每个原始 LoCoMo message 都应该有稳定编号；每个记忆条目应能知道来自哪些 message。后续 UPDATE/MERGE 即使改写了旧记忆，也必须能够知道本次变化涉及哪些直接消息和历史继承消息。

这部分应尽量是**纯算法/数据库图遍历**，不依赖 LLM 精确判断。LLM 可以做语义充分性判断，但不能成为定位链路唯一依据。

### 2.4 Failure 的第一步是排除“模型能力不够”

不要直接看到错答就开始修检索。先将 Runtime 最终实际看见的 Memory 一并交给强 Maintenance 模型：

1. 判断这些可见信息是否足够支持参考答案；
2. 强模型盲答一次（不看 reference/prediction）；
3. 若信息充分且强模型能答对，则更可能是弱 Runtime 模型推理/回答能力问题，而不是 Access 或 Construction 问题。

只有排除这一情况，才深入追踪 raw conversation、Memory 构建、版本演变和检索。

### 2.5 Skill 仅保留三字段，并且必须真的被检索到

Skill 格式固定为：

```json
{
  "name": "短名称",
  "description": "何时应检索到它",
  "content": "检索到之后的可执行策略"
}
```

每个问题可创建/更新/复用 Skill，但新 Skill 不能只“写出来”：必须通过 Runtime 同一检索器的自然检索闸门。若没有被检索到，只修改 `description`；若已检索到但修复无效，只修改 `content`；直到成功或预算耗尽。

Construction failure 只围绕**第一个断点**反复修，不能在同一个 Skill 里顺手修改下游多个环节。

### 2.6 数据集与公平评测

基准使用 LoCoMo conversation-level 6:2:2 split。训练集可学习 Skill；
validation 只比较并选取 Bank 版本；test 完全冻结，不能调用 Failure 或
Skill-Maker。当前通过穷举平衡 QA 数量与 QA category 分布，而非按文件
顺序截取：train/validation/test 分别为 1200/392/394 个 QA。生成脚本是
`single_agent_mim/scripts/create_locomo_split.py`。

Base 与 MiM 比较时，Runtime 模型、Memory、检索、预算、数据切分都必须一致；差异只能是 MiM 是否获得选中的 Skill。

## 3. 当前实现的架构

```text
raw conversation
    │
    ▼
Construction Agent (weak)
    │  candidates + ADD/UPDATE/MERGE/SKIP
    ▼
SQLite Memory Store
    │
    ├── raw messages / sessions
    ├── construction inputs / candidates / decisions
    ├── versioned memory / parent edges / message lineage
    ├── change events
    └── access actions / retrieval hits / answer-visible context
    │
    ▼
Access & Answer Agent (weak)
    │  prediction + exact final context
    ▼
Failure Workflow (strong semantics + deterministic SQL provenance)
    │  first broken edge + learning route
    ▼
Skill-Maker Workflow
    │  CRUD → staging → forced replay → retrieval gate
    │  → natural replay → regression → publish
    ▼
versioned Skill Bank
```

### 3.1 事实 Memory 的唯一真相源

文件：`single_agent_mim/src/mim/storage/sqlite_store.py`

每次运行产生一个独立数据库：

```text
outputs/<run_id>/state/memory.sqlite3
```

Memory 不再使用 JSON store。JSON 仅作为最终快照/人类阅读 artifact，不能被当成运行真相源。

核心表的责任：

| 表/关系 | 用途 |
|---|---|
| `messages`, `sessions` | 原始会话与稳定 message ID |
| `construction_inputs` | 哪些 raw message 进入某次构建 |
| `memory_candidates` | Extraction 的候选事实 |
| `candidate_message_edges` | candidate 的直接 message 来源 |
| `construction_decisions` | ADD/UPDATE/MERGE/SKIP 原因 |
| `memory_versions` | 不可原地覆盖的记忆版本 |
| `memory_version_parent_edges` | 版本之间的父子演变 |
| `memory_lineage_messages` | 当前版本继承的全部 message 来源 |
| `memory_change_events` | 每次改写涉及的直接/受影响消息 |
| `access_*` | 检索、最终可见答案上下文和最终 evidence |

### 3.2 Message ID 规则

LoCoMo 的原始 `dia_id` 会在不同 sample 中重复。现在统一使用：

```text
<conversation_id>:<dia_id>
```

QA evidence 做同样转换。因此 ID 既稳定又可作为全局 SQLite 主键，不能再回退成裸 `D1:1`。

### 3.3 三个 workflow

| 命令 | 目的 | 是否调用 Maintenance |
|---|---|---|
| `python main.py use` | 导入一段会话并问答 | 否 |
| `python main.py train` | 训练 conversation 上诊断失败、生成并验证 Skill | 是 |
| `python main.py evaluate` | 冻结 Bank 在 validation/test 上评测 | 否 |
| `python main.py smoke` | 无 API Key 的端到端 SQLite 健康检查 | 否，Mock |

所有 workflow 共用 `MiMRuntime`，不要为 train/use/evaluate 各自复制一套 Memory 或 Agent 编排。

## 4. Failure：当前已落实的工作流

完整实现：

- `single_agent_mim/src/mim/failure/workflow.py`
- `single_agent_mim/src/mim/failure/provenance.py`
- `single_agent_mim/src/mim/failure/first_break.py`

每个失败会写：

```text
outputs/<run_id>/failures/<failure_id>_report.json
```

其中 `stage_outputs` 保留阶段返回。顺序如下：

| 阶段 | 做什么 | 关键返回 |
|---|---|---|
| S0 | 冻结 case、snapshot、Access run、gold IDs | `visible_version_ids`, `answer_prompt_hash` |
| S1-A | 强模型判断 Runtime 最终可见 Memory 是否充分 | `runtime_context_sufficiency`, 支持版本、缺失 claim |
| S1-B | 强模型只看相同 Memory 盲答 | blind answer + 正确性 |
| S2 | 原始 gold messages 是否支持 reference | `raw_support`, 支持 message、缺失 claim |
| S3 | 纯 SQL 从 message 追踪到版本与 Access | `SourceTrace[]`, structural breaks |
| S4 | 强模型判断冻结 snapshot Memory 是否充分；算法覆盖矩阵 | snapshot sufficiency + coverage |
| S5 | 取实际检索和最终答案上下文 | access action IDs / retrieved / context / evidence IDs |
| S6 | 取相关版本的改写事件 | version change events |
| S7 | 从左到右找第一断边并路由 | label, subtype, `first_broken_edge`, route |
| S8 | 序列化最终报告 | confidence/review/route |

第一断边的主要分类：

```text
message_to_construction / message_to_candidate /
candidate_to_decision / decision_to_version / version_to_version
    → Construction Skill-Maker

memory_to_retrieval / retrieval_to_answer_context /
answer_context_coverage
    → Access Skill-Maker

answer_context_to_answer
    → Runtime model failure / record only
```

## 5. Skill-Maker：当前已落实的工作流

实现目录：`single_agent_mim/src/mim/skill_maker/`

关键路径：

```text
Failure report
  → draft (LLM)
  → deterministic payload validation
  → REUSE / CREATE / UPDATE planning
  → isolated staging bank
  → forced replay
  → natural retrieval gate
  → natural replay
  → regression replay
  → immutable bank publish
```

关键约束：

- Candidate 不能直接写进 active Bank；
- staging 目录会自动清理；
- 每个 candidate 的初稿与每次 revision 写入 JSONL；
- 每次 publish 产生 `bank_vNNN.json`；
- `selected.json` 是 validation 选出的冻结 Bank；
- payload validator 会拒绝 reference answer、message/memory ID 和 case-specific narrative 泄漏；
- Access 与 Construction 都有 forced/natural/retrieval/regression 路径；
- Construction 的目标断边固定，不允许 scope creep。

## 6. 已完成工作

### 6.1 TASK1 已完成的设计与基础交付

原始任务定义了单 Agent MiM demo、四 Agent、LoCoMo 6:2:2、Skill-Maker 和版本化 Memory。相关历史设计文档：

- `AGENT/TASK1_RESULT.md`
- `AGENT/TASK1_RUNTIME_ARCHITECTURE.md`
- `AGENT/TASK1_FAILURE_ARCHITECTURE.md`
- `AGENT/TASK1_SKILL_MAKER_ARCHITECTURE.md`

### 6.2 TASK2 已完成的工程收敛

TASK2 发现旧代码存在“文档是新版，但运行路径仍混有旧 JSON/legacy API”的问题，已经完成以下处理：

1. 统一 train/use/evaluate 到 SQLite Runtime；
2. 删除旧 JSON Memory、旧 replay、旧 legacy retrieval 与旧 Prompt；
3. 修复 SQLite 成功路径漏 commit、FTS closed connection、provenance 不回填版本等问题；
4. 修复 LoCoMo `dia_id` 跨会话重复；
5. 记录 Construction 输入、Candidate、Decision、Version、lineage、change event；
6. 记录 Access 的 exact action/hit/final visible context/prompt hash/evidence；
7. 将 Failure 和 Skill-Maker 接入真实训练链，而不是孤立模块；
8. 将回归检测从固定 `True` 占位改成真实 replay；
9. 重写测试，覆盖 use/train/evaluate/Failure/Skill-Maker/SQLite 的新版行为；
10. 添加 README、完整架构交接、重大改动报告。

不要恢复以下已删除旧路径：

```text
src/mim/memory.py
src/mim/replay.py
src/mim/retrieval.py
src/mim/retrieval/legacy.py
prompts/construction.md
prompts/failure.md
prompts/skill_maker.md
```

## 7. 新会话应先阅读的文件

推荐阅读顺序：

1. `AGENT/CONVERSATION_RESTART_HANDOFF.md`（本文件）；
2. `single_agent_mim/README.md`（入口、命令和产物）；
3. `single_agent_mim/docs/ARCHITECTURE_AND_HANDOFF.md`（完整技术说明）；
4. `AGENT/TASK2_MAJOR_CHANGE_REPORT.md`（为何删除旧架构、已修的风险）；
5. 用户提出的新任务对应的 `AGENT/TASK*.md`；
6. 再针对问题查看具体模块和测试。

代码导航：

| 想理解什么 | 先看哪里 |
|---|---|
| CLI / 命令参数 | `single_agent_mim/main.py` |
| 配置与模型 provider | `single_agent_mim/configs/default.yaml`, `src/mim/config.py`, `src/mim/llm/` |
| Runtime 总编排 | `src/mim/workflows/use.py` |
| train / validation Bank 选择 | `src/mim/workflows/train.py` |
| frozen evaluation | `src/mim/workflows/evaluate.py` |
| Memory schema 与事务 | `src/mim/storage/schema.sql`, `sqlite_store.py` |
| Construction / Access LLM 调用 | `src/mim/agents/` |
| Failure 诊断 | `src/mim/failure/` |
| Skill 生命周期 | `src/mim/skill_maker/`, `src/mim/skills.py` |
| 契约测试 | `single_agent_mim/tests/` |

## 8. 如何与用户协作

### 8.1 沟通偏好

用户使用中文，关注的是研究/架构是否真正闭环，而不只是在乎表面文件数量。交流时应：

- 先给结论，再说明理由；
- 直接指出发现的架构风险、数据契约冲突或“文档和代码不一致”；
- 对于小到中等范围的实现，直接完成，无需反复确认；
- 对于大的改动，也可以直接完成，但要额外生成一份清晰的报告；
- 如果存在会明显改变研究方向的真实分歧，应停下来说明证据、备选方案和影响，再与用户讨论；
- 不要为了“最小”而删除用户已明确要求的可维护性、溯源或评测闭环。

用户曾明确说过：

> “直接完成任务无需确认，如果有大的改动直接多做一个报告即可。”

因此通常应自行做合理工程判断；只有涉及目标扩张、数据/模型权限、研究定义改变时才请求决定。

### 8.2 实现原则

1. 优先维护一个真相源，而不是增加兼容层；
2. Runtime 行为必须在 use/train/evaluate 三处一致；
3. 结构定位尽可能硬编码/纯算法，语义判断才用 LLM；
4. 对 LLM 输出做 JSON 解析、预算限制、可追踪记录；
5. 所有新增 artifact 必须说明写到哪里、由谁读、生命周期是什么；
6. 修改后至少跑相关 pytest；涉及主链时跑 `python main.py smoke`；
7. 任何实际模型调用都要说明其成本、API 配置和不可重复性。

### 8.3 提交/交付方式

完成一个有实质影响的任务时，交付应至少说明：

- 修改了哪些模块；
- 数据格式或存储是否变化；
- 新工作流的输入/输出；
- 已做的验证与其范围；
- 未验证的真实外部依赖；
- 如改动较大，新增的交接/迁移报告路径。

## 9. 当前已知边界与合理后续方向

这些不是 bug，而是有意保留的 MVP 边界：

1. 单进程串行评测，不是并发生产服务；
2. embedding 存在 SQLite BLOB 中，规模很大时可替换向量层，但不应丢掉 provenance；
3. 多跳 QA 的 gold evidence 目前按单一 evidence unit，未来可做 claim 拆分；
4. regression buffer 当前只在一个 train 进程中驻留，跨 run 恢复尚未实现；
5. 真实外部模型 API 的完整 train/evaluate 尚未做成本型实验；
6. `SkillRepository` 是 JSON immutable versioned Bank，足够评测，不是多人协同 Skill 服务。

如用户要求下一步研究增强，优先顺序建议是：

1. 用真实强弱模型跑小规模 LoCoMo train/validation，检查 prompt 与成本；
2. 检查 Failure report 的真实质量及 first-break 分布；
3. 对多跳问题拆分 evidence unit；
4. 让 regression buffer 跨 run 保存；
5. 再考虑规模化向量检索或并发。

## 10. 最低自检命令

在修改代码后从 `single_agent_mim` 目录运行：

```powershell
python -m compileall -q src tests main.py
python -m pytest -q
python main.py smoke
```

预期：测试全部通过，smoke 输出 `status: passed`，并显示 SQLite 的 messages/candidates/decisions/versions/answer_context 非零计数。

## 11. 权威文档层级

若文档之间有细节矛盾，优先顺序为：

1. 用户在当前对话中的最新明确指令；
2. 当前代码与测试的数据契约；
3. `single_agent_mim/docs/ARCHITECTURE_AND_HANDOFF.md`；
4. `AGENT/TASK2_MAJOR_CHANGE_REPORT.md`；
5. TASK1 的历史设计文档。

历史文档用于理解设计来源，但不得覆盖当前用户的新决定或现行代码契约。

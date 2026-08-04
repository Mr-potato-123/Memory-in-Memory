# Memory in Memory（MiM）：从失败轨迹中学习可复用记忆策略

> **状态说明：本文件是工程与论文混合的中间稿。最终使用时请分别参考
> `MiM_当前实现与学术映射报告.md` 和 `MiM_论文正文草稿.md`。**

> **副标题：面向大模型介导记忆系统的错误驱动程序性元记忆层**  
> **文档性质：论文 Introduction、方法正文与当前技术实现统一稿**  
> **实现基线：Unified Single-Agent Memory MVP，LoCoMo 版本**

---

## 0. 文档说明

本文不是对 `idea_v3.md` 的简单压缩，而是根据当前已经落地的
Single-Agent MVP、Diagnosis V3、批量 Skill Bank 工作流和近期技术选型讨论，
重新组织的一版论文正文草稿。

本文严格区分三类内容：

1. **已经实现的系统能力**：当前 `single_agent_mim` 中可以由代码和数据结构直接对应的部分；
2. **已经确定但仍需正式实验验证的方法主张**：例如 Skill 是否能够自然召回并提高未见测试集表现；
3. **论文后续扩展**：LangMem-Agentic、MIRIX 等外部基座适配及跨基座实验。

这种区分非常重要。MiM 当前已经完成一个可运行、可追踪、可诊断、可生成
Skill Bank 的最小闭环，但尚不能把“跨基座普适性”“持续多轮提升”或“显著优于强基座”
写成已被实验确认的结论。

---

# 摘要

长期记忆使大语言模型智能体能够跨会话保存用户事实、状态变化和历史事件，但现有系统
主要积累的是“记住了什么”，而很少持续积累“以后应该如何记忆”。当下游回答出现错误时，
常见做法是扩大检索范围、重新生成摘要、修补当前事实，或将失败案例写入一个新的经验库。
这些方法通常不能回答两个更基础的问题：错误究竟发生在记忆构建、记忆访问，还是回答模型
对证据的利用阶段；一次修复又能否被抽象为未来案例可复用的记忆操作经验。

本文提出 Memory in Memory（MiM），一种面向大模型介导记忆系统的错误驱动程序性
元记忆层。MiM 将记忆系统分解为记忆构建与记忆访问两个策略阶段，并在正常运行之外建立
一个离线控制平面。系统首先使用语义 Judge 确认回答错误，再以严格隔离的数据权限分别执行
回答充分性诊断、访问故障诊断和构建故障诊断。访问诊断只比较当前已有的有用记忆与自然
搜索链实际返回的记忆；构建诊断则先判断当前记忆是否缺失或错误，仅在必要时沿原始消息、
构建候选、决策、提交和版本变更定位最早错误点。

对于确认的访问或构建故障，MiM 将诊断结果抽象为结构简洁的自然语言 Skill。候选 Skill
与正式 Skill Bank 物理隔离，经语义聚类、候选—正式 Skill 全矩阵召回、批量 CRUD、
写集合冲突检测和事务化发布后，形成可版本化的正式 Skill Bank。运行时只读取冻结的正式
Skill，并记录被选中的 Skill、未选中的近邻、分数和 Bank 版本，使下一轮诊断能够区分
“没有相应策略”“策略没有被召回”和“策略被使用但仍然无效”。

当前原型使用 Qwen3-8B non-thinking 作为运行模型，使用 DeepSeek-V4-Pro 执行
Judge、Diagnosis 和 Skill 学习；以 SQLite、FTS5、Sentence-Transformers、
BM25、关键词检索、结构化时间检索和加权 RRF 构成完全可观测的记忆基座，并在
LoCoMo 上采用 conversation-level 6:2:2 划分。该系统的目标不是提出新的底层存储，
而是验证：下游错误能否被可靠定位为记忆策略错误，以及这些错误能否形成可检索、可维护、
可复用的程序性元记忆。

---

# 第一部分：Introduction

## 1. 长期记忆系统缺少的不是另一个事实库

大语言模型的上下文窗口可以容纳越来越长的输入，但长期运行的智能体仍需要外部记忆。
原因不仅是上下文长度有限，还包括成本、检索效率、跨会话状态保持、用户画像更新和历史
事实管理等问题。于是，近年来的智能体记忆系统逐渐形成一条典型流水线：

```text
原始交互
   ↓
信息选择与记忆构建
   ↓
持久化记忆状态
   ↓
查询规划与记忆访问
   ↓
证据组织与回答
```

这些系统可以使用向量数据库、文档摘要、结构化事实表、时间图或多种存储的组合。然而，
存储结构的增强并不会自动消除记忆错误。只要大模型参与“写什么、如何更新、查什么、
何时停止”中的任一决策，系统就会持续出现策略性失败：

- 重要事实没有形成持久记忆；
- 相对时间没有被解析为可查询的绝对时间；
- 新状态覆盖旧状态时破坏了历史信息；
- 两条主题相似但语义不同的记忆被错误合并；
- 目标事实已经存在，却因问题表述变化而没有被召回；
- 多跳问题只找到其中一跳便提前回答；
- 正确条目被召回，但旧版本或相似事件被错误地排在前面。

现有系统通常将这些问题视为一次性的运行错误。它们可能重试当前查询、扩大 top-k、
重写摘要、修改当前事实，或把当前失败写入日志。但系统很少把修复过程转化为一种长期资产：

> 当未来再次出现相似记忆情形时，系统应该如何改变自己的构建或访问策略？

因此，本文关注的不是增加第三个事实记忆库，而是在对象级记忆之外增加一个
**程序性元记忆层**。

---

## 2. 对象级记忆与程序性元记忆

对象级记忆回答：

```text
用户是谁？
发生了什么？
某个状态在何时有效？
过去有哪些事件？
```

程序性元记忆回答：

```text
遇到相对时间时应如何构建记忆？
更新当前状态时应如何保留历史？
列表问题的信息不足时应如何继续搜索？
多个候选版本冲突时应检查哪些时间约束？
```

二者的区别不在于是否使用自然语言保存，而在于被记忆的对象不同。对象级记忆保存世界
和用户事实；程序性元记忆保存记忆系统自己的操作经验。

本文将后者表示为 Skill。Skill 不应包含某个训练问题的具体答案，而应描述一类可复用的
触发情形和处理原则。例如，从“某人昨天失去工作，但系统将事件日期写成消息日期”这一具体
错误中，系统不应学习某人的失业日期，而应学习：

> 当消息包含相对时间表达时，使用消息时间作为锚点解析事件时间；不要把观察时间直接当成
> 事件发生时间，也不要在后续改写中删除尚未解析的时间限定。

---

## 3. 为什么需要显式故障归因

从错误答案直接生成经验存在一个根本问题：同一错误表象可能来自完全不同的阶段。

假设标准答案需要事实 \(f\)，运行结果错误：

- 如果 \(f\) 已经进入回答上下文，问题主要在回答模型；
- 如果 \(f\) 存在于当前记忆，但没有进入搜索结果，问题在访问策略；
- 如果原始对话包含 \(f\)，但当前记忆缺失或错误，问题在构建策略；
- 如果原始对话本身不支持 \(f\)，则可能是数据、标注或问题定义错误。

若不先归因，系统可能用扩大检索修复错误构建，也可能用修改构建提示词修复纯回答错误。
这种错误修复不仅浪费调用，还会把相互矛盾的规则写入同一个 Skill Bank。

MiM 因此采用两个原则：

1. **先诊断，再学习 Skill**；
2. **访问故障与构建故障是两个相互独立的诊断流程，而不是一个总分类器中的互斥标签。**

一个问题可以同时存在访问故障和构建故障。例如，答案需要事实 \(f_1\) 和 \(f_2\)：
当前记忆错误地丢失 \(f_1\)，同时虽然保存了 \(f_2\)，自然搜索链却没有返回它。此时，
构建侧和访问侧应分别形成修复对象，不需要创建含义模糊的 `combined_failure`。

---

## 4. 核心思路

MiM 的核心思路可以概括为：

> 把错误答案视为观察信号，沿“原始交互—记忆版本—访问链—回答上下文”反向定位根因，
> 再将具体修复抽象为可检索、可版本化、可合并的自然语言记忆策略。

完整链路为：

```text
运行时回答
   ↓
LLM-as-Judge：C / P / I
   ↓ 仅 P/I
Answer Diagnosis：已见证据是否充分
   ↓
Access Diagnosis  ||  Construction Diagnosis
   ↓                         ↓
访问修复包                  构建修复包
   ↓                         ↓
Access Candidate Skill     Construction Candidate Skill
   └──────────────┬──────────┘
                  ↓
         分侧聚类与批量 CRUD
                  ↓
       版本化 Official Skill Bank
                  ↓
       下一轮 Runtime 自然召回与评测
```

---

## 5. 论文贡献

本文计划围绕以下贡献组织，而不是把“实现了一个记忆 Demo”作为主要贡献：

1. **错误驱动的程序性元记忆问题定义。**  
   将智能体长期记忆中的持续改进对象，从用户事实扩展到“如何构建和访问记忆”的策略知识。

2. **严格隔离的记忆故障诊断。**  
   将回答、访问和构建诊断分成权限不同的独立流程，避免诊断模型使用不属于该阶段的信息。

3. **基于来源追踪和版本链的构建首错定位。**  
   每个记忆版本保留来源消息、父版本和变更事件，使系统能够定位信息第一次被遗漏或破坏的位置，
   而不是只观察最终错误状态。

4. **访问与构建分治的双 Skill Bank。**  
   两类 Skill 分别指导 Access & Answer Agent 和 Construction Agent，并在运行、诊断、
   候选生成和 CRUD 中保持方向隔离。

5. **候选—正式 Bank 隔离的批量 Skill 演化机制。**  
   通过候选聚类、全矩阵相似 Skill 召回、多操作 CRUD、冲突重规划和事务发布，减少逐问题
   更新造成的版本爆炸和反复覆盖。

6. **一个完全可观测的最小研究基座与可扩展实验协议。**  
   当前原型提供可重放 SQLite 状态、混合检索、ReAct 访问、Judge-first 训练和冻结评测，
   后续以 LangMem-Agentic 和 MIRIX 检验外部可插拔性。

---

# 第二部分：问题定义

## 6. 大模型介导的记忆系统

本文研究一类大模型介导的智能体记忆系统：

\[
\mathcal{M}
=
\left(
\mathcal{D},
\pi_{\mathrm{con}},
\pi_{\mathrm{acc}},
f_{\mathrm{ans}}
\right)
\]

其中：

- \(\mathcal{D}\) 是可持久化、可查询的记忆状态；
- \(\pi_{\mathrm{con}}\) 是大模型参与的记忆构建策略；
- \(\pi_{\mathrm{acc}}\) 是大模型参与的记忆访问策略；
- \(f_{\mathrm{ans}}\) 根据问题和访问证据形成答案。

给定第 \(t\) 个会话片段 \(x_t\)，构建过程为：

\[
D_t,\tau_t^{\mathrm{con}}
=
\pi_{\mathrm{con}}
\left(
x_t,D_{t-1},S_t^{\mathrm{con}}
\right)
\]

其中 \(S_t^{\mathrm{con}}\) 是为该会话召回的 Construction Skills，
\(\tau_t^{\mathrm{con}}\) 是候选抽取、相关记忆读取、CRUD 决策和版本变更轨迹。

给定问题 \(q\)，当前实现将记忆访问和回答合并为同一个连续 ReAct 过程：

\[
\hat{y},\tau_q^{\mathrm{acc}}
=
\pi_{\mathrm{acc+ans}}
\left(
q,D_t,S_q^{\mathrm{acc}}
\right)
\]

合并的原因是：访问是否充分只有在模型读取搜索结果后才能判断。模型应该能够在同一个上下文中
继续搜索、检查条目或直接回答，而不是由外部工作流预先规定固定搜索次数。

---

## 7. Skill 的最小定义

当前 MVP 不采用 `trigger + operator plan + invariants + validation criteria`
等大而完整的 Skill Schema，而使用更易运行和维护的最小结构：

```json
{
  "name": "Short human-readable name",
  "description": "When this Skill should be retrieved.",
  "content": [
    "One or more concise and actionable instructions."
  ]
}
```

其中：

- `name` 用于人类审计；
- `description` 是主要检索表示，说明何时应调用；
- `content` 是注入 Agent 的操作性自然语言规则。

候选 Skill 额外保存一个简短的 `solves` 字段：

```json
{
  "solves": "A short paragraph explaining the general failure pattern solved."
}
```

`solves` 不进入 Runtime，而是作为 Candidate Agent 与 Batch CRUD Agent 之间的信息边界。
它使 CRUD 能理解候选想解决什么，又不需要读取完整诊断包、原始对话或记忆版本历史。

---

## 8. 三类诊断结果

### 8.1 Answer Failure

运行模型已经获得支持标准答案所需的充分信息，但仍然产生错误答案。此类结果只记录，不生成
记忆 Skill，因为问题不属于记忆构建或访问。

### 8.2 Access Failure

当前记忆中存在对标准答案有用的条目，但自然搜索链没有把这些条目全部返回给回答模型。

设：

- \(M_{\mathrm{useful}}\) 为当前快照中对答案必要或有用的记忆条目集合；
- \(M_{\mathrm{retrieved}}\) 为整条自然搜索链实际返回的当前记忆集合。

则：

\[
M_{\mathrm{missing}}
=
M_{\mathrm{useful}}
\setminus
M_{\mathrm{retrieved}}
\]

当 \(M_{\mathrm{missing}}\neq\varnothing\) 时，存在 Access Failure。

### 8.3 Construction Failure

原始对话证据支持标准答案，但当前相关记忆没有完整、正确地保留该信息。Construction
Diagnosis 不只给出“当前记忆错误”，还应沿构建轨迹找到最早错误点：

\[
t^*
=
\min
\left\{
t \mid
\text{required information is first omitted or corrupted at step }t
\right\}
\]

如果信息在首次抽取时就错误，修复目标是 extraction；如果首次写入正确，后来一次
UPDATE、MERGE 或 DELETE 首次破坏信息，则修复目标是该变更操作。

---

# 第三部分：系统总体架构

## 9. 数据平面与控制平面

MiM 将正常记忆运行与错误学习明确分离。

### 9.1 数据平面

```text
LoCoMo Session
      ↓
Construction Skill Retrieval
      ↓
Memory Construction Agent
      ↓
Versioned SQLite Memory State
      ↓
Access Skill Retrieval
      ↓
Access & Answer ReAct Agent
      ↓
Prediction + Search Trace + Visible Evidence
```

数据平面只负责构建记忆和回答问题。它不读取 gold answer，也不更新候选 Skill。

### 9.2 控制平面

```text
Prediction + Reference
          ↓
Semantic Judge
          ↓
Answer / Access / Construction Diagnosis
          ↓
Candidate Skill Generation
          ↓
Candidate Clustering + Bank Retrieval
          ↓
Batch CRUD + Conflict Replanning
          ↓
Official Skill Bank Publication
```

控制平面只在训练或开发阶段使用参考答案和 evidence。测试阶段冻结全部 Skill、
提示词、模型和检索参数，不允许根据测试错误回写 Bank。

---

## 10. 当前智能体角色

当前实现包含七个清晰的智能体角色，但可以归纳为运行、诊断和学习三层。

| 层级 | 智能体 | 职责 |
|---|---|---|
| Runtime | Construction Agent | 从会话抽取候选，结合相关记忆执行 ADD/UPDATE/MERGE/DELETE/SKIP |
| Runtime | Access & Answer Agent | 在同一 ReAct 上下文中自主搜索、检查记忆并回答 |
| Diagnosis | Answer Diagnosis Agent | 判断运行时已经可见的信息是否充分；只记录 |
| Diagnosis | Access Diagnosis Agent | 比较当前有用记忆与自然搜索结果，定位漏搜 |
| Diagnosis | Construction Diagnosis Agent | 筛查当前记忆，并沿来源和版本链定位第一个构建错误 |
| Skill Learning | Candidate Skill Agent | 从一个诊断包生成一个候选 Skill，或判断无需更新 |
| Skill Learning | Batch Skill CRUD Agent | 对一组候选和相关正式 Skill 执行批量整理计划 |

这里的“Access & Answer 合并”是刻意设计，而不是少实现一个 Agent。访问模型在获得每次
搜索结果后，应该自行判断证据为 FULL、PARTIAL 或 NONE，并决定继续搜索还是回答。
将 Access 和 Answer 拆成两个上下文反而会丢失自然搜索链的连续性。

---

# 第四部分：Runtime 数据平面

## 11. 记忆构建

### 11.1 两阶段构建

每个会话先经过候选抽取，再经过批量记忆管理决策。

候选抽取输出：

```text
memory_kind
subject
predicate
object_text
content
world_start / world_end
source_message_ids
entities
keywords
importance
confidence
```

构建管理器对每个候选选择：

```text
ADD
UPDATE
MERGE
DELETE
SKIP
```

- `ADD` 创建新的逻辑记忆；
- `UPDATE` 在同一 `memory_id` 下产生新版本；
- `MERGE` 将新证据整合进已有逻辑记忆；
- `DELETE` 使当前版本失效，但保留历史；
- `SKIP` 用于重复、短暂、无支持或已经充分表示的信息。

构建管理不是把相似候选自动合并。候选只能修改本轮实际暴露给模型的目标记忆；
一个目标在同一批次最多被一个候选声明。无效目标会被确定性降级，避免模型凭空修改未见条目。

### 11.2 世界时间与系统时间

当前数据模型区分：

- `world_start/world_end`：事实在对话世界中的有效时间；
- `system_from_commit/system_to_commit`：该记忆版本在系统版本链中的有效区间。

这种双时间设计使系统既能回答“事实何时发生”，也能追踪“系统何时开始以某种方式保存它”。

### 11.3 来源追踪

每个候选和记忆版本均关联 `source_message_ids`。当一个记忆被更新或合并时，系统保留：

- 直接触发当前变化的消息；
- 从父版本继承的历史来源；
- 父版本关系；
- change event；
- before/after 版本。

因此，给定 LoCoMo evidence message ID，程序可以反向找到：

```text
evidence message
   ↓
candidate extraction
   ↓
construction decision
   ↓
memory version
   ↓
all later changes involving its lineage
```

这一链路为 Construction Diagnosis 提供了纯算法可遍历的证据骨架。LLM 负责判断语义是否
丢失或被破坏，程序负责 ID、版本、父子关系和时间顺序。

---

## 12. SQLite 记忆状态

当前 MVP 选择 SQLite，而不是引入独立向量数据库或图数据库。该选择服务于研究可观测性：

- 一次运行一个独立数据库，避免实验状态串扰；
- 事务可保证构建批次全部成功或全部回滚；
- 外键和版本表适合保存来源、父版本和 change event；
- FTS5 提供本地全文检索；
- embedding 可以作为归一化向量编码保存；
- WAL 模式支持诊断阶段的并发只读；
- 数据规模较小，精确扫描比额外的近似索引更容易复现。

核心表按功能分为：

```text
原始数据：
  conversations
  sessions
  messages

构建轨迹：
  construction_commits
  construction_inputs
  memory_candidates
  candidate_message_edges
  construction_decisions

记忆版本：
  memory_versions
  memory_version_message_edges
  memory_version_parent_edges
  memory_lineage_messages
  memory_change_events
  memory_change_parents
  memory_fts

访问轨迹：
  access_runs
  access_actions
  access_retrieval_hits
  access_answer_context
  access_final_evidence

评测标注：
  qa_cases
  qa_gold_sources
```

`access_runs.skill_trace_json` 和 `construction_commits.skill_trace_json`
额外记录运行时 Skill 召回，使诊断不仅知道“记忆怎么被处理”，还知道“当时有哪些元记忆
规则进入了 Agent 上下文”。

---

## 13. Access & Answer ReAct

### 13.1 自然搜索链

Access & Answer Agent 在一个连续消息上下文中工作。可用动作是：

```text
search_memory
inspect_memory
answer
```

每个工具结果完整保留在同一上下文。模型可以在第一次搜索后直接回答，也可以更换查询、
检索路线或约束继续搜索，直到它认为信息充分或预算耗尽。

系统不强制“至少搜索两次”或“固定多步检索”。这种设计保留了用户强调的自然 ReAct 逻辑：

> 提示模型可以持续搜索，信息不足时继续；信息已经足够时立即回答。

### 13.2 多路检索工具

Agent 可以独立选择：

```text
semantic
bm25
keyword
structured
temporal
hybrid
```

一次查询可同时携带：

- 语义 query；
- 最多四个 query expansions；
- 精确关键词；
- 实体；
- memory kind；
- 当前、历史或目标时间约束；
- shallow / standard / deep 检索深度；
- 是否包含历史版本；
- top-k。

这使 Access Skill 可以影响模型如何组织查询和选择工具，而不是把所有问题都压缩成固定的
向量相似度检索。

### 13.3 Hybrid Retrieval

混合检索由四条独立候选路线组成：

```text
Semantic embedding       0.40
BM25                     0.30
Keyword exact matching   0.15
Structured / temporal    0.15
```

不同排名通过加权 Reciprocal Rank Fusion 合并：

\[
\mathrm{RRF}(m)
=
\sum_{r\in\mathcal{R}}
\frac{w_r}{k+\operatorname{rank}_r(m)}
\]

当前 \(k=60\)。融合后再应用可解释的乘数：

```text
entity match             × 1.10
target-time valid        × 1.20
current active           × 1.05
temporal mismatch        × 0.50
```

最终按逻辑 `memory_id` 去重；只有明确要求历史时，才允许同一逻辑记忆的多个版本共同返回。

### 13.4 停止与证据约束

Agent 对每轮结果判断：

```text
FULL      → 回答
PARTIAL   → 针对缺失事实继续检索
NONE      → 改变查询、路线或过滤条件
```

回答动作必须提供可见的 `evidence_version_ids`。程序验证这些版本是否确实出现在本轮搜索或
检查结果中，防止模型引用不可见证据。

---

## 14. Runtime Skill 召回

Runtime 只读取 `official/` 中处于 active 状态的 Skill。Access 和 Construction 按
`side` 严格过滤。

### Access Skill Query

使用完整问题作为查询，在 ReAct 开始前将 Top-k Skill 注入系统上下文。

### Construction Skill Query

使用完整会话的 `speaker: content` 文本作为查询，将同一组 Skill 提供给候选抽取和
构建决策。

### 排序与披露

当前正式 Skill 排序为：

```text
85% semantic similarity
15% lexical overlap
```

默认真正加载 Top-3，同时记录之后 5 个未加载近邻。Skill trace 包含：

- 查询文本和方向；
- Official Bank 版本；
- 被加载 Skill 的不可变快照；
- 未被加载近邻的不可变快照；
- rank、总分、语义分和词法分。

被加载 Skill 影响当前行为；近邻只用于诊断和下一轮 Candidate 生成。这样可以在失败后判断：

- 是否根本不存在相关 Skill；
- 相关 Skill 是否只差一点进入 Top-k；
- 已经进入上下文的 Skill 是否仍不足以解决问题。

---

# 第五部分：错误诊断控制平面

## 15. Judge-first 路由

MiM 不使用 Token F1 决定一个回答是否进入诊断。LoCoMo 的开放式、时间和列表答案会使
词面 F1 对合理同义表达过于敏感。

系统首先使用点式、reference-guided 的 LLM-as-Judge 输出：

```text
C = Correct
P = Partially Correct
I = Incorrect
```

Judge 只判断答案质量，不读取记忆、搜索链或构建历史。仅 `P/I` 进入后续诊断。

时间问题使用 LoCoMo 的虚构对话时间线和 evidence timestamp，禁止用现实当前日期解释
对话中的“去年”“昨天”等表达。

---

## 16. Answer Diagnosis

Answer Diagnosis 只读取运行时模型真实看见的上下文：

- 问题；
- 参考答案；
- 运行时答案；
- Judge 标签；
- 每一步搜索或检查结果；
- 最终可见记忆正文。

它不读取未检索的当前记忆、raw conversation、构建历史或其他诊断结果。

模型将参考答案拆成必要事实，并判断每个事实是否被可见记忆支持。若全部必要事实已被支持，
却仍然得到 `P/I`，则记录 Answer Failure。

Answer Failure：

- 不生成修复包；
- 不进入 Skill-Maker；
- 不阻止 Access 和 Construction 继续独立诊断。

---

## 17. Access Diagnosis

Access Diagnosis 的权限被刻意限制在当前记忆。

它可以读取：

- 问题和参考答案；
- evidence message 通过来源边反查到的当前 active 记忆；
- 自然搜索链返回过的当前 active 记忆；
- 搜索步骤和条目 ID；
- 当时的 Access Skill trace。

它不能读取：

- raw conversation；
- evidence 原文；
- 历史版本；
- 父版本和 lineage；
- 构建 candidate、decision、commit；
- Construction Diagnosis 结果。

模型负责判断哪些当前记忆对必要事实有帮助，程序负责集合差：

\[
\text{Access Failure}
\Longleftrightarrow
M_{\mathrm{useful}}
\setminus
M_{\mathrm{retrieved}}
\neq \varnothing
\]

修复包保留漏掉的条目、实际检索结果和每次搜索链，但不要求诊断模型直接给出新查询、
关键词、过滤条件或权重。如何修复属于后续 Skill 学习，不属于诊断。

---

## 18. Construction Diagnosis

Construction Diagnosis 采用渐进式权限开放。

### 阶段 A：当前记忆筛查

只读取问题、参考答案和当前相关记忆。对必要事实输出：

```text
FULL
PARTIAL
MISSING
INCORRECT
```

如果所有必要事实均为 FULL，则结束，不读取原始对话和历史。

### 阶段 B：首错溯源

仅当阶段 A 确认存在构建问题时，程序才加载：

- 标注的 raw evidence message；
- 相关抽取候选；
- ADD/UPDATE/MERGE/DELETE/SKIP 决策；
- commit 和 change event；
- 父版本；
- before/after 记忆；
- 处理这些来源消息时使用的 Construction Skill trace。

诊断按时间检查：

```text
raw evidence 是否支持参考事实
   ↓
候选是否正确抽取
   ↓
决策是否选择正确操作和目标
   ↓
首次落盘是否正确
   ↓
后续哪次变更第一次破坏信息
```

若存在多个错误，只输出最早错误。修复后，后续错误是否仍存在应在下一轮重新构建和诊断，
而不是一次向 Skill Agent 塞入一长串相互依赖的修复目标。

---

## 19. 权限隔离

| 数据 | Answer | Access | Construction A | Construction B |
|---|---:|---:|---:|---:|
| 问题与参考答案 | 是 | 是 | 是 | 是 |
| 运行时答案 | 是 | 否 | 否 | 否 |
| 实际检索结果 | 是 | 是，仅当前版本 | 否 | 否 |
| 未检索的当前相关记忆 | 否 | 是 | 是 | 是 |
| raw evidence | 否 | 否 | 否 | 是 |
| 历史版本与父版本 | 否 | 否 | 否 | 是 |
| 构建候选和决策 | 否 | 否 | 否 | 是 |
| 其他诊断结果 | 否 | 否 | 否 | 否 |

这一权限表不是文档约定，而是诊断 evidence view 和 workflow 层的工程契约。

---

## 20. 模型判断与确定性算法的边界

交给大模型的部分：

- 参考答案的必要事实分解；
- 记忆内容是否语义支持某个事实；
- 当前记忆是否完整或错误；
- 最早构建变化为什么破坏了信息；
- 具体错误可以抽象为什么通用策略。

交给程序的部分：

- message、memory、version、commit 和 change ID 查询；
- 当前版本过滤；
- 集合并、差和去重；
- 父版本与来源边遍历；
- commit 时间排序；
- 最早错误候选选择；
- Schema、ID 和权限验证；
- 并发、重试、恢复和文件写入。

原则是：

> 语义判断交给模型，身份、集合、顺序、版本关系和状态变更交给算法。

---

# 第六部分：Skill 学习与 Skill Bank 演化

## 21. Candidate Skill 生成

每个有效的 Access 或 Construction 诊断包独立调用 Candidate Skill Agent。它读取：

- 完整诊断包；
- 当时真正进入 Runtime 的 Skill；
- 排名靠后但未进入 Runtime 的近邻 Skill；
- 对应 Official Bank 版本。

Agent 可以返回：

```text
PROPOSE_SKILL
NO_CHANGE_ALREADY_COVERED
NO_CHANGE_NOT_A_SKILL_PROBLEM
```

第二种结果很重要：某个错误可能已经被现有 Skill 覆盖，只是该轮没有正确召回或执行。
此时继续新增 Skill 会制造重复。第三种结果则防止将数据问题、回答问题或一次性异常写入 Bank。

Candidate 内容必须去除具体人物、日期、答案、message ID 和 memory ID，只保留一般性情形
与操作指导。

---

## 22. 候选与正式 Bank 的物理隔离

```text
skills/
├── official/
│   ├── banks/
│   │   ├── bank_v000.json
│   │   ├── bank_v001.json
│   │   └── ...
│   └── selected.json
├── candidates/
│   ├── access/<candidate-id>/
│   └── construction/<candidate-id>/
└── transactions/
```

隔离规则：

- Runtime 只读取 `official/selected.json`；
- Candidate Agent 只能写 `candidates/<side>`；
- Access 和 Construction 候选物理分区；
- 候选不能通过移动文件直接成为正式 Skill；
- 只有通过确定性校验的事务可以产生新 Official Bank 版本；
- 历史 Bank 版本不可变。

该设计允许 Candidate 高并发产生，同时避免未审查规则污染在线 Runtime。

---

## 23. 为什么不逐问题立即 CRUD

若每个诊断包都立即修改 Skill Bank，会出现：

- 大量高度重复的新 Skill；
- 同一个 Skill 被连续反复改写；
- Bank 版本数量与错误数近似线性增长；
- 后一个问题看到的 Bank 与前一个问题不同，实验顺序影响显著；
- 并发写入难以处理。

因此，当前系统先独立生成所有 Candidate，再做批量整理。Candidate 保留它想解决的问题摘要，
而 CRUD 只负责把候选整理为一个简洁、一致的正式 Bank。

---

## 24. Candidate 聚类

Access 与 Construction 分别聚类。Candidate 表示由三个部分组成：

```text
description embedding   45%
content embedding       35%
solves embedding        20%
```

系统使用确定性的 spherical K-means：

\[
K
=
\left\lceil
\frac{N_{\mathrm{candidate}}}
{N_{\mathrm{target}}}
\right\rceil
\]

当前目标组大小为 8，普通 CRUD 批次最多 10 个 Candidate。

K-means 后再进行规则修正：

- 指向同一 Official Skill 的 Candidate 应尽量进入同组；
- 词法高度重合的 Candidate 应尽量进入同组；
- 超大组按批次上限切分。

不只使用 `name` 聚类，因为名称短、信息量小且容易受生成措辞影响。

---

## 25. Candidate × Official Skill 统一召回

对于每个候选组，系统不把十个候选拼接成一条巨型查询，而是显式计算：

\[
R \in
\mathbb{R}^{N_{\mathrm{candidate}}\times N_{\mathrm{official}}}
\]

相似度由：

```text
description semantic similarity   50%
content semantic similarity       30%
BM25 lexical similarity           20%
```

共同组成。

检索策略保证：

- 每个 Candidate 至少保留若干自己的相关邻居；
- Candidate 在 Runtime trace 中明确关联的 Official Skill 强制保留；
- 对整个组都相关的公共 Skill 被补入；
- Official 上下文受统一上限约束，当前为 25。

当初始 Bank 为空时，空检索结果是合法状态，CRUD 可以在一个事务中创建多个新 Skill。

---

## 26. Batch CRUD

Batch CRUD Agent 只读取：

- Candidate Skill；
- `solves`；
- Candidate—Official 相似度关系；
- 被统一召回的 Official Skill。

它不读取：

- 完整诊断包；
- raw conversation；
- memory lineage；
- Runtime search trace；
- gold evidence。

可用操作为：

```text
add_skill
rename_skill
update_description
add_content
update_content
delete_content
move_content
delete_skill
```

每个 Candidate 必须获得恰好一个 resolution：

```text
CREATED
MERGED_INTO_EXISTING
MERGED_INTO_CANDIDATE
ALREADY_COVERED
NOT_A_SKILL_PROBLEM
REJECTED
```

LLM 只输出计划，不直接写文件。确定性执行器检查：

- base Bank 版本是否冻结；
- Candidate 是否全部被处理且无重复；
- CRUD 目标是否真实存在并被允许读取；
- expected Skill version 是否匹配；
- content index 和 expected old content 是否匹配；
- 是否发生 Access/Construction 跨方向修改；
- 新 Skill Schema 是否有效。

---

## 27. 冲突检测与事务发布

同一方向的所有语义组先基于同一个冻结 Bank 规划。程序计算每份计划的写集合：

\[
W_i
=
\{
\text{skill IDs modified by plan }i
\}
\]

若 \(W_i\cap W_j\neq\varnothing\)，两个计划存在写冲突。系统使用并查集把所有相连冲突
合并成组件，重新统一召回并调用 CRUD Agent，直到剩余写集合互不相交。

冲突解除后，同一方向的操作合并成一个 release transaction：

```text
Access       → 每轮至多发布一个新 Bank 版本
Construction → 每轮至多发布一个新 Bank 版本
```

正式 Bank 是联合版本，但每个事务仍然受 side 限制。例如从空 Bank 开始，可能形成：

```text
v000  empty
v001  Access release
v002  Access + Construction cumulative release
```

---

## 28. Skill 效果验证

当前 MVP 不在同一批 Candidate 上反复修改，直到原错误强制通过。这是近期设计相对于
`idea_v3` 和早期 `idea_v4` 的重要收束。

原因是：

- 逐错误强制修复容易把 Skill 过拟合到单个诊断包；
- 每个错误反复更新会重新引入版本爆炸；
- 真正需要验证的是下一轮自然 Runtime 中能否召回并减少同类错误。

当前主线验证分为：

1. **静态与事务验证**：Schema、方向、版本、CRUD 和冲突是否合法；
2. **Validation Bank 选择**：在 validation conversation 上比较不同 Bank 版本；
3. **下一轮自然调用验证**：观察 Skill 是否进入 Top-k、是否只出现在近邻、对应错误是否减少；
4. **冻结测试**：选择 Bank 后在 test conversation 上一次性运行。

强制调用、逐 Candidate 自然调用和局部回归仍可作为后续扩展或消融，但不应被写成当前
批量 MVP 已经完整执行的默认步骤。

---

# 第七部分：当前完整技术栈

## 29. 技术组件总表

| 层 | 当前选择 | 作用 | 选择理由 |
|---|---|---|---|
| 开发语言 | Python 3.12 | Runtime、Diagnosis、Skill、评测 | 生态成熟，便于模型 API、数据和实验脚本统一 |
| 数据建模 | Pydantic 2 | Config、Agent 输出、Skill、报告 | 强 Schema、运行时校验、JSON 序列化 |
| 配置 | PyYAML + 环境变量解析 | 模型、预算、检索和提示词路径 | 实验参数可冻结、可哈希、可复现 |
| 模型 SDK | OpenAI Python SDK、Anthropic SDK | OpenAI-compatible 与 Claude 接入 | 用统一角色接口适配不同模型服务 |
| 运行存储 | SQLite + WAL + Foreign Keys | 记忆、版本、轨迹、QA | 单运行隔离、事务、可追踪、无需额外服务 |
| 词法索引 | SQLite FTS5 + 自实现 BM25 | exact/BM25 检索 | 可解释且适合小规模研究数据 |
| 向量表示 | Sentence-Transformers `all-MiniLM-L6-v2` | 记忆和 Skill 语义相似度 | 本地 CPU、成本低、结果稳定 |
| 数值计算 | NumPy | embedding、cosine、矩阵检索、K-means | 小 Bank 上可做精确全矩阵计算 |
| 文本指标 | NLTK | Token F1 的规范化与词干处理 | 与词面评测兼容并减少简单形态差异 |
| Runtime LLM | Qwen3-8B，non-thinking | Construction、Access & Answer | 成本可控、保留优化空间、工具调用能力足够 |
| Maintenance LLM | DeepSeek-V4-Pro | Diagnosis、Candidate、CRUD | 使用更强模型处理离线语义判断和抽象 |
| Judge | DeepSeek-V4-Pro，temperature 0 | C/P/I 语义评测 | 避免开放答案仅由 Token F1 决定 |
| LLM 接口 | OpenAI-compatible + Anthropic adapter + Mock | 可替换 Qwen/GPT/Claude/DeepSeek | Runtime 与 maintenance 解耦 |
| 提示词 | English Markdown prompts | 所有模型输入 | 降低混合语言协议错误，方便交给 coding agent |
| 数据集 | LoCoMo | 跨会话长期记忆 QA | 有 message-level evidence，适合来源追踪和诊断 |
| 进度与产物 | JSON/JSONL + manifest + hash | 断点恢复、审计、复现实验 | 每个阶段可独立检查 |
| 并发 | `ThreadPoolExecutor` | Judge、Diagnosis、Candidate API 调用 | API I/O 场景下实现有界并发 |
| 运行进度 | tqdm + JSONL events | conversation/QA 进度与机器审计 | 人类监控和断点恢复分离 |
| 测试 | Pytest + MockClient | Runtime、版本、诊断权限、Skill 事务 | 离线验证不消耗模型 API |
| 入口 | `main.py` + thin scripts | use/train/evaluate/diagnosis/skill pipeline | 业务逻辑集中在 `src/mim`，脚本只编排 |

---

## 30. 模型分工

Runtime 与 maintenance 使用不同模型和完全独立的消息上下文：

```text
Qwen3-8B non-thinking
  → 正常记忆构建
  → 正常访问与回答

DeepSeek-V4-Pro
  → LLM-as-Judge
  → Answer / Access / Construction Diagnosis
  → Candidate Skill
  → Batch CRUD Plan
```

这一分工不是要求 MiM 必须依赖两个特定模型，而是为了建立清晰实验角色：

- 较弱、较便宜的 Runtime 暴露真实可优化空间；
- 较强的 maintenance 模型承担离线诊断和抽象；
- 两者共享 API 凭证与否不影响上下文隔离；
- 后续可替换为 GPT、Claude、Qwen 或其他兼容接口。

Qwen3-8B 显式设置 `enable_thinking=false`，避免 reasoning 输出破坏 JSON 工具协议，
也保证和已有 non-thinking 基线一致。

---

## 31. 为什么当前不使用外部向量数据库

LoCoMo 只有十个长对话，当前 Official Skill Bank 规模也预期有限。使用 FAISS、
Milvus、Pinecone 或 Elasticsearch 并不会自动提高研究可信度，反而会增加：

- 版本和来源关系跨系统同步；
- 数据库重置和实验隔离成本；
- 近似索引非确定性；
- 并发服务依赖；
- 复现环境复杂度。

因此，当前实现采用 SQLite 保存结构状态，以 NumPy 对当前快照 embedding 做精确点积，
并对 Skill Bank 计算完整 Candidate × Official 矩阵。只有当跨数据集和跨用户实验使规模
显著增长时，才需要替换为 ANN 或专门向量存储。

---

## 32. LLM 适配层

模型客户端抽象支持：

```text
openai_compatible
anthropic
mock
```

OpenAI-compatible 接口用于 DashScope Qwen、DeepSeek 及其他兼容服务；
Anthropic adapter 为 Claude 保留独立接入；MockClient 用于确定性测试。

配置层支持：

- model；
- api key 或环境变量；
- base URL；
- temperature；
- max tokens；
- timeout 和 retries；
- JSON mode；
- provider-specific `extra_body`；
- reasoning 输出控制。

因此，MiM 的方法依赖的是“运行模型”和“维护模型”的角色，而不是某家模型供应商。

---

# 第八部分：工程组织与可复现性

## 33. 当前活动代码结构

```text
single_agent_mim/
├── configs/
├── data/splits/
├── docs/
├── exp/
│   └── single-mem/raw/
├── outputs/
├── prompts/
│   ├── access.md
│   ├── construction_extraction.md
│   ├── construction_decision.md
│   ├── diagnosis/
│   ├── failure/                 # legacy compatibility
│   ├── judge/
│   └── skill_maker/
├── reports/
├── scripts/
│   ├── judge_predictions.py
│   ├── run_answer_failure.py
│   ├── run_access_failure.py
│   ├── run_cons_failure.py
│   └── run_skill_bank_pipeline.py
├── src/mim/
│   ├── agents/
│   ├── diagnosis/
│   ├── eval/
│   ├── llm/
│   ├── retrieval/
│   ├── skill_maker/
│   ├── storage/
│   ├── workflows/
│   ├── config.py
│   ├── schemas.py
│   └── skills.py
├── tests/
└── main.py
```

`main.py` 负责正常使用、兼容训练、冻结评测和 smoke；三类 Diagnosis 使用独立薄入口；
正式 Judge-first Skill 学习由 `run_skill_bank_pipeline.py` 编排。现存
`main.py train` 仍保留早期按 Token F1 路由的兼容流程，不能替代正式 Judge-first
实验入口。核心算法不复制到脚本中，而集中在 `src/mim`。

---

## 34. 运行隔离

每次运行创建独立目录：

```text
outputs/<run-id>/
  config.resolved.yaml
  manifest.json
  state/memory.sqlite3
  qa_results.jsonl
  traces/
  failures/
  skills/
  summary.json
```

相对 SQLite 路径总是解析到当前 run directory。正式评测不复用另一个 run 的可变数据库，
从而避免两个 conversation 或 Base/MiM 之间共享状态。

产物规则：

- `outputs/` 保存可恢复的运行状态；
- `exp/single-mem/raw/` 保存被接受的最终原始实验结果；
- `docs/` 保存英文开发和 Agent 指南；
- `reports/` 保存中文用户报告；
- 所有正式结果保存 resolved config、prompt hash、dataset hash 和 source hash。

---

## 35. 并发与恢复

### Diagnosis

Answer 先运行；完成后 Access 和 Construction 可以并行。二者使用独立输出目录、独立
progress、errors、summary 和 manifest，不建立 combined 状态。

### Candidate

Candidate 生成可以有界并发，每个诊断包一个独立上下文。已完成、no-change 和错误记录
按 diagnosis ID 恢复，避免重复 API 调用。

### CRUD

CRUD 规划可以按候选组产生，但所有计划必须基于同一冻结 Bank。程序在发布前检测写集合
冲突；Official Bank 的最终修改是串行、事务化的。

### Judge

Judge 按小批次并发，输出按 QA ID 去重并支持 resume。最终必须验证输入输出行数、唯一 ID、
合法标签和永久错误数。

---

# 第九部分：实验协议

## 36. LoCoMo conversation-level 6:2:2

LoCoMo 只有十个 conversation。若把同一个 conversation 的问题随机分到 train、validation
和 test，人物、事件、原始消息和记忆状态会发生严重泄漏。因此当前使用 conversation-level
固定划分：

```text
Train:
  conv-30, conv-42, conv-43, conv-44, conv-48, conv-49

Validation:
  conv-26, conv-41

Test:
  conv-47, conv-50
```

随机种子为 42，split 文件绑定 LoCoMo 数据文件 SHA-256。

训练集只用于：

- 运行答案；
- Judge-first 错误筛选；
- Diagnosis；
- Candidate 和 Skill Bank 学习。

验证集只用于：

- Bank 版本选择；
- 提示词、检索预算和阈值选择；
- 失败分析和消融开发。

测试集只用于冻结后的最终评测。测试结果不得更新 Skill Bank。

---

## 37. 两种评价口径

### Token F1

保留与主流 LoCoMo 论文可比较的词面指标，并按类别处理列表、时间和不可回答问题。

### LLM-as-Judge

以 C/P/I 作为主要语义口径：

```text
C：必要事实完整正确，无关键矛盾
P：包含部分正确信息，但缺失必要事实或存在有限偏差
I：错误、矛盾、不响应或错误拒答
```

本文不把 LLM-as-Judge 转换为一个伪精确的 F1。主要报告：

- C/P/I 数量与比例；
- 按 conversation 和 question category 的分布；
- Base 到 MiM 的 C、P、I 迁移；
- Token F1 作为独立的第二口径。

Judge-first 诊断使用 C/P/I，而不是 Token F1 阈值；Bank validation 以 C rate 为主，
I rate 和 Token F1 为次级选择依据。

---

## 38. 主实验基座

根据“构建和访问是否真正由 Agent 策略介导”“是否可追踪”“是否能保持公平工具预算”等讨论，
论文主实验拟采用三层证据：

### 38.1 Unified Single-Agent Memory

即当前实现。作用是：

- 提供完全可控的机制实验；
- 精确检查来源、版本、搜索链和 Skill trace；
- 完成错误归因和 Skill 机制消融；
- 不是外部知名度证据。

### 38.2 LangMem-Agentic

使用官方 hot-path tool mode：

- `manage_memory` 由 Agent 自主 create/update/delete；
- `search_memory` 由 Agent 自主决定查询、过滤、重复搜索和停止；
- 不启用 native prompt optimizer 作为默认 Base；
- MiM 只通过外层 policy/Skill 注入，不改写底层 Store。

其作用是证明 MiM 可以增强成熟工程 SDK，而不是只在自建基座上有效。

### 38.3 MIRIX

保留其 Meta Memory Manager、六类 Memory Manager 和 Chat Agent：

- Construction Skill 影响 memory-type routing、更新和去重；
- Access Skill 影响 component、retrieval method、targeted search 和停止；
- 不改变 MIRIX 原有六类 memory schema；
- 固定原生工具、轮数和预算。

其作用是测试 MiM 在强多智能体、异构记忆系统上的边际价值。

三者形成：

```text
机制可控基座
  + 经典工程基座
  + 强多智能体系统基座
```

---

## 39. 直接竞争与辅助实验

### 直接竞争

- **MemSkill**：最接近 Construction Skill Bank，但主要学习构建技能，依赖 PPO selector；
- **PlugMem**：task-agnostic memory plugin，应与 MiM 横向比较，不做插件套插件；
- **MemMA**：为 storage backend 增加当前实例上的 Meta-Thinker、query refinement 和
  fact repair，与 MiM 的跨实例双 Skill Bank 形成对照。

### Access 压力测试

- MemMachine + MiM-Access；
- MRAgent + MiM-Access；
- Hindsight + MiM-Access。

这些系统的 construction 或 access 不完全满足联合主实验门槛，因此用于单侧压力测试，
不支持“联合优化”主张。

### 静态 Access 辅助实验

对于 LightMem、A-MEM、MemoryOS 等固定检索系统，需要加入：

```text
Native Backend
Backend + Generic Access Agent
Backend + Generic Access Agent + Global Reflection
Backend + MiM
```

否则无法区分收益来自“新增 Agent loop”还是来自 MiM 的错误归因和 Skill 学习。

---

## 40. 公平性约束

每个 `Base` 与 `Base + MiM` 必须保持：

- 相同 Runtime 模型；
- 相同底层 Memory DB 和 schema；
- 相同原生工具集合；
- 相同最大步骤和工具调用预算；
- 相同上下文和 token 预算；
- 相同回答提示词；
- 相同数据划分；
- 相同 Judge。

MiM 唯一新增变量应是：

- Skill 的离线学习；
- Runtime 前的 Skill 检索和注入；
- 对应的 trace 记录。

不能通过给 `+MiM` 更多搜索次数、更强回答模型或额外检索工具获得不公平优势。

---

## 41. 核心实验组

在 Unified Single-Agent 上至少比较：

```text
Base
Base + More Retrieval
Base + Retrieved Failure Cases
Base + Global Reflection Prompt
Base + MiM-Construction only
Base + MiM-Access only
Base + MiM-Joint
```

核心判断不是只看 Joint 的总分：

- Construction Failure 是否下降；
- Access Failure 是否下降；
- Answer Failure 是否被错误地写成 Skill；
- Bank 是否出现重复和增长失控；
- Joint 是否稳定优于两个 single-side 版本；
- 新 Skill 是否在未见 conversation 中自然召回。

---

## 42. 关键消融

1. 无故障归因，直接从错误答案生成 Skill；
2. Access 与 Construction 使用同一个 Skill Bank；
3. 不披露 Runtime Skill trace；
4. Candidate 只看诊断包，不看 selected/nearby Skill；
5. 每个错误立即 CRUD，而不是批量聚类；
6. 只按 embedding 聚类，不使用 `solves` 和词法修正；
7. CRUD 读取完整诊断包，而不是只读取 Candidate；
8. 只允许 ADD，不允许更新、合并和删除；
9. 不检测跨批次写冲突；
10. Candidate 直接进入 Runtime，不做物理隔离；
11. 只报告 Token F1，不使用语义 Judge；
12. 随机 QA 切分替代 conversation-level 切分；
13. 原始失败案例检索替代抽象 Skill；
14. 单一持续增长的全局提示词替代 Skill Bank。

其中最重要的是：

- 无故障归因；
- 失败案例检索；
- 全局提示词；
- 单 Bank；
- 逐问题 CRUD。

这五组直接回答 MiM 是否只是普通 reflection、case retrieval 或 prompt accumulation。

---

## 43. 需要报告的指标

### 端到端

- Token F1；
- C/P/I；
- 各 LoCoMo 类别；
- protocol error；
- Runtime token 和 latency；
-平均 Access steps 和 Construction steps。

### 诊断

- Answer/Access/Construction 数量；
- Access 漏搜条目数；
- Construction 首错阶段分布；
- review-required 和 data issue；
- 诊断包完整率；
- 人工抽查一致性。

### Skill

- proposed/no-change/error；
- created/merged/covered/rejected；
- Access/Construction Skill 数量；
- Candidate 聚类数和 CRUD 批次数；
- 每版本更新量；
- Bank redundancy；
- description 召回率；
- selected/nearby/effective；
- Skill 对未见错误的复用次数。

### 成本

- 每个 Runtime QA 的调用数和 token；
- 每个诊断包的 maintenance 调用；
- 每个 Candidate 和 CRUD batch 的调用；
- Bank 构建总费用；
- 相对于 Base 的推理延迟增加。

---

# 第十部分：与相关方法的边界

## 44. 与 Mem0 类方法的关系

当前记忆构建在工程上借鉴了 Mem0 风格的简洁事实存储和
ADD/UPDATE/DELETE 管理思想，但 MiM 的研究对象不是如何建立一个更好的事实集合。

关键区别是：

- Mem0 类系统主要维护对象级 memory；
- MiM 额外维护 Construction/Access procedural Skill；
- MiM 从回答失败反向诊断；
- MiM 保存来源、版本和首错轨迹；
- MiM 的 Candidate 和 Official Bank 有独立生命周期。

---

## 45. 与 Letta/MemGPT 的关系

Letta/MemGPT 证明了 Agent 可以通过工具主动管理和访问持久记忆，并影响当前上下文。
当前 Access & Answer ReAct 的自然工具循环与这一方向一致。

但 MiM 不把“有一个会调用 memory tool 的 Agent”当作贡献。MiM 关注的是：

- Agent 为什么在某类案例中反复调用错误；
- 如何从失败中形成可复用策略；
- 如何在未来类似输入前检索这些策略；
- 如何区分策略不存在、没有召回和召回后无效。

---

## 46. 与 MemSkill 的关系

MemSkill 与 MiM-Construction 最接近：二者都把自然语言 Skill 用于记忆构建。

MiM 的不同点在于：

- 从下游失败和显式根因归因出发；
- 同时维护 Access Skill；
- 不要求以 PPO selector 作为核心；
- Diagnosis、Candidate 和 Official Bank 物理分层；
- 使用当前 Runtime 的 selected/nearby Skill trace 避免重复学习；
- 批量 CRUD 负责全局整理，而不是每个难例立即改 Bank。

因此，MemSkill 是最重要的 construction-side competitor，而不是适合被 MiM 再套一层的基座。

---

## 47. 与 MemMA 的关系

MemMA 在 storage backend 外增加 Meta-Thinker、Memory Manager 和 Query Reasoner，
主要在当前实例上诊断 evidence gap、重写查询，并用 repair fact 修补对象级记忆。

MiM 的长期积累对象不同：

```text
MemMA：修复后的当前事实 Memory DB
MiM：以后应如何构建或访问的程序性 Skill Bank
```

MiM 还要求把 Access 与 Construction 错误独立诊断，并通过下一轮自然 Skill trace
观察策略是否真正被调用。

---

## 48. 与全局 Reflection Prompt 的关系

全局 reflection 把所有经验不断追加或压缩到一个系统提示词中。它实现简单，但会产生：

- 上下文持续增长；
- 不相关规则总是进入每个问题；
- 相互冲突经验难以定位；
- 无法知道某次行为由哪条经验触发；
- 修改一条规则需要重写整个提示词。

MiM 的 Skill 是条件检索、方向分离、版本化和可 CRUD 的。论文必须通过
Global Reflection baseline 证明这种结构化管理确实优于一个持续膨胀的提示词。

---

# 第十一部分：当前实现边界与研究路线

## 49. 当前已经实现

当前 Unified Single-Agent MVP 已具备：

- LoCoMo conversation ingestion；
- message-level 稳定 ID；
- 版本化 SQLite memory；
- source、parent、lineage 和 change event；
- Construction Agent 的抽取与批量 CRUD；
- Access & Answer 的自然 ReAct 搜索链；
- semantic/BM25/keyword/structured/temporal 混合检索；
- Qwen3-8B non-thinking Runtime；
- DeepSeek-V4-Pro Judge 和 Diagnosis；
- Answer、Access、Construction 独立权限；
- Construction 首错定位；
- Runtime selected/nearby Skill trace；
- Candidate 与 Official Skill 物理隔离；
- Candidate 聚类、全矩阵 Bank 召回；
- 多操作 Batch CRUD；
- 写冲突合并和事务化 Bank 发布；
- conversation-level 6:2:2；
- Token F1 与 C/P/I 两种评价接口；
- MockClient 与离线测试体系；
- JSONL、manifest、hash 和断点恢复。

---

## 50. 尚需正式验证

以下内容已经有工程接口或明确方案，但不能在论文中写成实验结论：

- 第一轮完整 Candidate 和 Skill Bank 的真实 API 生成质量；
- Skill 在 validation/test 上的自然召回率；
- MiM 相对 Base 的 C/P/I 与 Token F1 增益；
- Access-only、Construction-only 与 Joint 的比较；
- Skill Bank 重复率和版本振荡；
- Candidate 批量 CRUD 是否比逐问题更新更稳定；
- 使用同一个 Skill 修复多个未见案例；
- 多 Epoch 下的持续提升和回归控制。

---

## 51. 尚未实现的论文扩展

- LangMem-Agentic adapter；
- MIRIX adapter；
- MemSkill、PlugMem、MemMA 完整对比；
- MemMachine/MRAgent Access 压力测试；
- LongMemEval 或第二真实基准；
- 可控故障注入集；
- 多随机种子与显著性检验；
- 跨数据集和跨基座迁移；
- 强制调用、自然调用和局部回归的完整 per-Skill 验证门；
- 人工 Skill 抽象性、忠实性和泄漏性评测。

这些是论文完整版本的路线，不应成为当前 MVP 继续无限扩张的理由。当前优先任务仍是：

```text
完成第一轮 Skill Bank
   ↓
验证自然召回
   ↓
冻结 validation 选择
   ↓
完成 Base vs MiM test
   ↓
分析错误类型变化
```

---

# 第十二部分：论文叙事建议

## 52. 论文应当强调什么

论文的主语应是：

> 一个从下游错误中学习记忆构建和访问策略的外置程序性元记忆层。

正文应重点强调：

- 故障归因为什么必要；
- Access 和 Construction 为什么必须分开；
- 来源与版本追踪如何支持首错定位；
- Skill 为什么不是另一个事实；
- Candidate 为什么不能直接进入 Runtime；
- 批量 CRUD 为什么比逐错误更新稳定；
- selected/nearby Skill trace 如何使学习闭环可观察。

---

## 53. 论文不应当夸大什么

当前阶段不应写：

- MiM 已经适配任意记忆系统；
- Skill 已经证明能够跨基座迁移；
- 批量 CRUD 一定优于所有在线更新；
- DeepSeek 诊断等同于真实根因；
- LLM-as-Judge 是客观无偏的正确性标准；
- SQLite Single-Agent 是强于现有系统的新记忆架构。

更准确的表述是：

- MiM 提出可适配的策略层和数据契约；
- Unified Single-Agent 提供机制验证；
- 跨基座主张需由 LangMem-Agentic 和 MIRIX 实验支持；
- Judge、Diagnosis 和 Skill 质量需要人工抽查与消融；
- 底层记忆基座是研究仪器，不是论文主要创新。

---

## 54. 推荐的一句话贡献

> MiM 将大模型记忆系统的下游失败转化为可追踪的构建或访问故障，并把修复经验维护为可检索、
> 可版本化和可批量演化的程序性元记忆。

---

## 55. 推荐的 Introduction 收束段

本文提出 Memory in Memory（MiM），一个附加在大模型介导记忆系统之外的错误驱动
元记忆层。MiM 不改变底层事实存储的定义，而是观察记忆构建、版本演化、自然搜索和回答
上下文，在离线阶段将错误定位为回答利用、记忆访问或记忆构建问题。对于可修复的访问与构建
故障，系统生成方向独立的自然语言 Candidate Skill，并通过候选聚类、正式 Bank 召回、
批量 CRUD、冲突重规划和事务发布形成可维护的双 Skill Bank。运行时只检索冻结的正式
Skill，并保存 selected 与 nearby trace，使策略的存在、召回和效果能够在下一轮被分别
观察。通过这一设计，记忆系统不再只积累关于用户和世界的事实，也开始积累关于自己应该
如何记忆的程序性经验。

---

# 结论

Memory in Memory 的最准确定位不是“为记忆系统自动写提示词”，也不是“在记忆外再放一个
失败案例库”，而是：

> **一个以错误归因为入口、以双 Skill Bank 为长期资产、以自然调用轨迹为验证信号的
> 程序性元记忆控制层。**

当前技术实现已经把这一思想落为一个最小但完整的研究系统：

```text
可来源追踪的版本记忆
  + 自然 ReAct 访问
  + Answer / Access / Construction 隔离诊断
  + Candidate / Official Skill 隔离
  + 批量聚类与 CRUD
  + 冻结评测
```

下一步工作的重点不应继续增加更多 Agent 或更复杂的存储，而应完成第一轮正式 Skill Bank
实验，验证三个最基础的问题：

1. 诊断结果能否形成抽象而非案例化的 Skill；
2. Skill 能否在未见 conversation 中自然召回；
3. Access 与 Construction Skill 是否分别减少对应错误，并最终改善语义正确率。

只有在这三个问题得到可靠回答后，跨基座适配、多 Epoch 演化和更复杂 Skill 生命周期才有
充分的实验基础。

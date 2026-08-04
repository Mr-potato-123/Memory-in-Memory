# Memory in Memory（MiM）技术方案 v2

> **副标题：面向大模型介导记忆系统的错误驱动程序性元记忆层**  
> **文档性质：研究定位、系统设计、实验方案与技术路线**

---

## 0. 文档摘要

Memory in Memory（MiM）不是一种新的记忆存储结构，也不以替代现有记忆系统为目标。MiM 面向一类明确的研究对象：**由大语言模型参与记忆构建与记忆访问决策的智能体记忆系统**。这类系统可以使用向量数据库、文档存储、结构化记录或时间知识图谱作为底层记忆，但其“写什么、如何更新、查什么、如何筛选”至少部分由大模型策略控制。

MiM 位于基座记忆系统之外，作为一个**错误驱动的记忆控制层**。当下游回答出现错误时，MiM 联合检查原始交互、记忆状态、记忆构建轨迹、记忆访问轨迹和回答结果，对错误进行根因归因，并将成功的修复过程抽象为可复用的**程序性元记忆技能**。这些技能不保存用户事实本身，而是指导记忆系统在相似情境下应当如何构建、维护和访问记忆。

MiM 的核心对象包括两类策略：

1. **记忆构建策略**：负责从交互中识别、抽取、新增、修订、合并、失效和保持记忆；
2. **记忆访问策略**：负责查询规划、检索、过滤、重排、关联扩展、聚合和证据核验。

与现有版本一致，MiM 分别维护两套独立技能库，并保留错误池、技能检索、增删改并、版本状态、Preflight、强制调用验证、自然调用验证、局部重放和 Epoch 训练等机制。新版方案进一步完成以下收束：

- 将研究对象明确限定为“大模型介导的记忆系统”，避免对任意记忆架构做过度泛化承诺；
- 将原有 Writer/Reader 提升为“记忆构建策略/记忆访问策略”，Agent 作为策略执行载体；
- 定义基座无关的构建操作集与访问操作集，并通过适配器映射到具体系统；
- 将 Skill 定义为对抽象操作进行条件化选择、组合和约束的程序性元记忆；
- 将错误归因扩展为构建故障、访问故障、利用故障和无效样本，防止错误修复对象混淆；
- 增加局部验证、全局回归验证和安全上线条件，避免未验证技能污染共享技能库；
- 设计原生透明基座、经典工业基座和新强基座的分层实验，而非在三者中单选。

---

# 第一部分：研究定位

## 1. 研究对象

### 1.1 大模型介导的智能体记忆系统

本文考虑一类**大模型介导的智能体记忆系统**（LLM-mediated Agent Memory System）。其定义如下：

> 系统使用大语言模型对交互信息进行选择、抽取、整合、更新、失效或保留，并在任务执行时使用大语言模型规划查询、访问、筛选、聚合和组织记忆证据。

该定义不限定底层存储结构。一个系统可以使用：

- 向量数据库；
- 关键词倒排索引；
- 文档或摘要存储；
- 结构化事实表；
- 实体—关系图；
- 时间知识图谱；
- 多种存储的混合系统。

MiM 所要求的不是特定数据库，而是以下可控性和可观测性：

1. 记忆构建行为能够被提示词、工具调用、工作流或策略模块影响；
2. 记忆访问行为能够被提示词、工具调用、查询规划或排序规则影响；
3. 系统能够输出或近似重建构建轨迹与访问轨迹；
4. 系统能够读取当前记忆状态，或提供足以进行诊断的记忆查询接口；
5. 系统允许将抽象策略映射为本地操作。

### 1.2 不在本文范围内的系统

MiM 不直接面向：

- 完全不可观测的神经网络隐状态记忆；
- 无外部读写接口的端到端参数化记忆；
- 构建和访问行为均不可调节的封闭黑盒系统；
- 完全由确定性数据库规则执行、且没有大模型决策环节的普通信息系统；
- 只研究模型上下文窗口扩展、而不维护持久化记忆状态的方法。

这些边界不是实现缺陷，而是研究对象的必要限定。

---

## 2. 核心问题

长期运行的智能体记忆系统会重复出现相似错误。例如：

- 重要事实未被写入；
- 更新事实被错误合并或覆盖；
- 历史事实被错误删除；
- 当前事实与过期事实未区分；
- 目标事实已经存在，却因查询表达不同未被召回；
- 多条相关事实被召回，却未进行时间过滤或证据聚合。

现有系统通常只保存两类信息：

1. **对象级记忆**：用户事实、事件、状态、文档或环境信息；
2. **任务级轨迹**：某次交互中的动作、工具调用和结果。

但系统往往不会持续积累第三类信息：

> **关于“如何正确构建和访问记忆”的可复用操作知识。**

MiM 将这类知识称为**程序性元记忆**。

---

## 3. 核心研究假设

MiM 建立在以下假设之上：

### 假设 H1：下游错误可以被分解到记忆流水线中的不同阶段

通过联合观察原始交互、记忆状态、访问结果和回答结果，可以区分：

- 信息是否进入记忆；
- 记忆是否被正确维护；
- 正确记忆是否被访问；
- 正确证据是否被回答模型利用。

### 假设 H2：不同错误实例共享可抽象的修复结构

例如，“当前地址被旧地址覆盖”“当前职位返回旧职位”“当前饮食偏好返回旧偏好”，表面事实不同，但可能共享同一抽象技能：

> 当同一属性存在时间冲突版本时，应识别目标时间，保留历史版本，并优先访问在目标时间有效的事实。

### 假设 H3：程序性元记忆比原始错误轨迹更易复用

直接保存失败案例会携带具体实体、具体答案和具体基座操作。经过抽象后的 Skill 应当具备更好的：

- 跨用户复用；
- 跨问题表达复用；
- 跨数据集迁移；
- 跨基座适配；
- 可审计性与可编辑性。

### 假设 H4：读写分治比单一全局经验库更利于根因修复

记忆构建和记忆访问具有不同的状态、操作、失败模式与验证方式。将二者混合在一个全局提示词或经验库中，容易产生责任边界不清、错误技能调用和全局回归。

---

## 4. MiM 的定位

MiM 的最准确定位是：

> **一个面向大模型介导记忆系统的外置控制层，通过分析下游失败轨迹，学习、验证和维护用于修复记忆构建策略与记忆访问策略的程序性元记忆。**

MiM 不是：

- 新的向量数据库；
- 新的知识图谱；
- 单纯的记忆检索器；
- 单纯的提示词自动优化器；
- 把失败案例放入另一个向量库；
- 只针对某个现有记忆框架的定制补丁。

MiM 的研究贡献应集中在：

1. 记忆故障的可验证根因归因；
2. 构建与访问分治的程序性元记忆；
3. 从具体修复轨迹到抽象 Skill 的形成与演化；
4. 强制调用与自然调用分离的验证机制；
5. 基于抽象操作集和适配器的跨基座接入；
6. 技能复用、迁移和回归控制。

---

# 第二部分：统一系统抽象

## 5. 基座记忆系统

定义一个大模型介导的记忆系统为：

\[
\mathcal{M}=\left(\mathcal{D},\pi_{\mathrm{con}},\pi_{\mathrm{acc}},\Omega_{\mathrm{con}},\Omega_{\mathrm{acc}},f_{\mathrm{task}}\right)
\]

其中：

- \(\mathcal{D}\)：记忆状态或记忆存储；
- \(\pi_{\mathrm{con}}\)：记忆构建策略；
- \(\pi_{\mathrm{acc}}\)：记忆访问策略；
- \(\Omega_{\mathrm{con}}\)：记忆构建抽象操作集；
- \(\Omega_{\mathrm{acc}}\)：记忆访问抽象操作集；
- \(f_{\mathrm{task}}\)：使用访问证据完成回答或任务的模型。

### 5.1 记忆构建过程

给定第 \(t\) 个交互片段 \(x_t\) 和已有记忆状态 \(D_{t-1}\)：

\[
D_t,\tau_t^{\mathrm{con}}
=
\pi_{\mathrm{con}}
\left(
 x_t,D_{t-1};
 \Omega_{\mathrm{con}},K_{\mathrm{con}}
\right)
\]

其中：

- \(D_t\) 是更新后的记忆状态；
- \(\tau_t^{\mathrm{con}}\) 是构建轨迹；
- \(K_{\mathrm{con}}\) 是记忆构建技能库。

### 5.2 记忆访问过程

给定问题或任务请求 \(q\) 和当前记忆状态 \(D_t\)：

\[
C_q,\tau_q^{\mathrm{acc}}
=
\pi_{\mathrm{acc}}
\left(
 q,D_t;
 \Omega_{\mathrm{acc}},K_{\mathrm{acc}}
\right)
\]

其中：

- \(C_q\) 是返回给任务模型的记忆证据；
- \(\tau_q^{\mathrm{acc}}\) 是访问轨迹；
- \(K_{\mathrm{acc}}\) 是记忆访问技能库。

最终任务输出为：

\[
\hat{y}=f_{\mathrm{task}}(q,C_q)
\]

### 5.3 Agent 与策略的关系

正文中建议使用“策略”作为形式化对象，使用“智能体”作为实现对象：

- **记忆构建策略**由记忆构建智能体（Memory Construction Agent）执行；
- **记忆访问策略**由记忆访问智能体（Memory Access Agent）执行。

这种表述避免把系统限定为某一种 Agent 框架。一次带工具的大模型调用、多步工作流或显式规划智能体，都可以实现对应策略。

---

## 6. 数据平面与控制平面

### 6.1 数据平面：基座记忆系统的正常运行

```text
Raw Interaction / Conversation
              │
              ▼
Memory Construction Agent
              │
              ▼
        Memory State / MemDB
              │
              ▼
   Memory Access Agent
              │
              ▼
        Task / Answer Model
```

数据平面负责处理正常交互：构建记忆、访问记忆和完成任务。

### 6.2 控制平面：MiM 的错误学习过程

```text
Question + Feedback + Model Output
                │
                ▼
     Memory Failure Attributor
                │
      ┌─────────┼──────────┐
      ▼         ▼          ▼
Construction  Access    Utilization /
 Failure      Failure     Invalid
      │         │
      ▼         ▼
Construction  Access
Meta-Agent    Meta-Agent
      │         │
      ▼         ▼
Construction  Access
Skill Bank    Skill Bank
```

MiM 位于控制平面，主要职责包括：

- 判断错误是否属于记忆系统；
- 定位错误发生在构建还是访问阶段；
- 从失败证据中产生修复策略；
- 将修复策略抽象成 Skill；
- 验证 Skill 是否有效且可自然调用；
- 管理 Skill 的版本、合并、冻结和回归。

---

# 第三部分：抽象操作集与可插拔接口

## 7. 记忆构建操作集

定义基座无关的记忆构建操作集：

\[
\Omega_{\mathrm{con}}=
\{
\text{IDENTIFY},
\text{EXTRACT},
\text{ADD},
\text{REVISE},
\text{MERGE},
\text{INVALIDATE},
\text{PRESERVE},
\text{LINK},
\text{VERIFY},
\text{ABSTAIN}
\}
\]

| 操作 | 中文名称 | 作用 |
|---|---|---|
| IDENTIFY | 信息识别 | 判断交互中哪些内容值得形成持久记忆 |
| EXTRACT | 信息抽取 | 将原始表达转换为事实、事件、状态或结构化记录 |
| ADD | 新增 | 创建新的记忆条目 |
| REVISE | 修订 | 修改已有记忆的内容、属性、时间或置信度 |
| MERGE | 合并 | 将重复或互补记忆整合为统一表示 |
| INVALIDATE | 失效 | 标记旧事实在特定时间后不再有效，而非简单物理删除 |
| PRESERVE | 保持 | 在更新或合并时保留仍然有效的条件、历史与例外信息 |
| LINK | 关联 | 建立实体、事件、时间或因果关系 |
| VERIFY | 核验 | 检查写入结果是否忠实、完整、无冲突 |
| ABSTAIN | 跳过 | 判断当前内容不应写入或证据不足，停止构建 |

### 7.1 为什么使用 INVALIDATE 而不是仅使用 DELETE

长期记忆中的旧事实可能历史上正确、当前失效。例如：

- 用户过去居住在北京，目前居住在上海；
- 用户曾在公司 A 工作，目前在公司 B 工作；
- 用户过去不喝咖啡，目前接受无咖啡因咖啡。

直接删除旧事实会破坏历史问题回答。MiM 应优先学习“有效时间、状态更新和历史保留”，而不是简单覆盖。

---

## 8. 记忆访问操作集

定义基座无关的记忆访问操作集：

\[
\Omega_{\mathrm{acc}}=
\{
\text{FORMULATE},
\text{EXPAND},
\text{DECOMPOSE},
\text{RETRIEVE},
\text{FILTER},
\text{RERANK},
\text{TRAVERSE},
\text{AGGREGATE},
\text{VERIFY},
\text{ABSTAIN}
\}
\]

| 操作 | 中文名称 | 作用 |
|---|---|---|
| FORMULATE | 查询构造 | 从问题中提取实体、属性、时间和约束，形成查询 |
| EXPAND | 查询扩展 | 使用别名、同义词、上下位概念或相关表达扩展查询 |
| DECOMPOSE | 问题分解 | 将多跳、组合或时间问题拆成子查询 |
| RETRIEVE | 候选召回 | 从记忆存储获取候选条目 |
| FILTER | 条件过滤 | 根据实体、时间、有效性、来源和权限筛选候选 |
| RERANK | 候选重排 | 对候选进行语义、时间、来源或任务相关性排序 |
| TRAVERSE | 关联扩展 | 沿实体、事件或关系进行邻域和多跳访问 |
| AGGREGATE | 证据聚合 | 合并多条互补证据并处理重复或冲突 |
| VERIFY | 证据核验 | 检查证据是否足以回答问题、是否存在未解释冲突 |
| ABSTAIN | 拒绝访问结论 | 当记忆不足或冲突无法解决时返回不可回答信号 |

---

## 9. 基座适配映射

不同基座拥有不同的本地 API 和能力。MiM 通过适配器把抽象操作映射为本地操作：

\[
\phi_b^{\mathrm{con}}:
\Omega_{\mathrm{con}}
ightarrow\mathcal{A}_b^{\mathrm{con}}
\]

\[
\phi_b^{\mathrm{acc}}:
\Omega_{\mathrm{acc}}
ightarrow\mathcal{A}_b^{\mathrm{acc}}
\]

其中：

- \(b\) 表示具体基座；
- \(\mathcal{A}_b\) 表示基座真实可用的提示词、工具、API 和工作流动作。

### 9.1 示例映射

抽象操作 `INVALIDATE` 可以映射为：

- 简单向量记忆：更新元数据中的 `active=false` 或结束时间；
- 工业记忆框架：调用 update/delete，并保留历史版本副本；
- 时间知识图谱：设置事实有效区间终点；
- 文档式记忆：创建新版本并标记旧版本为过期。

抽象操作 `FILTER + RERANK` 可以映射为：

- 向量系统：元数据过滤后进行交叉编码器或大模型重排；
- 图系统：先执行时间约束和实体邻域遍历，再进行语义排序；
- 文档系统：先按会话和时间过滤，再进行段落级相关性判断。

### 9.2 可插拔契约

一个基座只要满足以下最低接口，即可接入 MiM：

```text
build(interaction, memory_state, construction_skills)
    -> new_memory_state, construction_trace

access(query, memory_state, access_skills)
    -> evidence, access_trace

inspect(memory_state, query_or_fact)
    -> diagnostic_candidates

restore(checkpoint)
    -> memory_state

compile(skill, local_capabilities)
    -> local_execution_plan
```

其中 `inspect` 可以是完整读取、高召回查询或受限诊断工具，不要求在线阶段暴露全部存储内容。

---

# 第四部分：程序性元记忆 Skill

## 10. Skill 定义

MiM 中的 Skill 不是用户事实，也不是底层数据库操作。其定义为：

> **针对一类记忆情境和故障模式，对多个抽象操作进行条件化选择、排序、组合和约束的可复用程序性策略。**

形式上：

\[
S=
\left(
T,F,P,I,V
\right)
\]

其中：

- \(T\)：触发条件（Trigger）；
- \(F\)：故障特征（Failure Signature）；
- \(P\)：抽象操作计划（Operator Plan）；
- \(I\)：不变量与执行约束（Invariants）；
- \(V\)：成功判据（Validation Criteria）。

### 10.1 Skill 与其他知识类型的区别

| 对象 | 保存内容 | 是否包含具体用户事实 | 是否直接绑定基座 |
|---|---|---:|---:|
| 对象级记忆 | 用户事实、事件、状态 | 是 | 通常是 |
| 原始错误轨迹 | 问题、答案、调用和状态 | 可能包含 | 通常是 |
| 全局提示词 | 总体行为规则 | 通常否 | 部分绑定 |
| MiM Skill | 可复用的构建/访问程序性策略 | 不应包含 | 抽象层不绑定 |

---

## 11. Skill Schema

建议将当前 `skill_name + skill_abstract + action` 扩展为以下结构，同时保留原有字段以兼容已有实现：

```json
{
  "skill_id": "uuid",
  "skill_name": "temporal_state_resolution",
  "skill_type": "access",
  "skill_abstract": "当同一属性存在多个时间版本时，根据问题目标时间过滤并核验有效事实。",
  "trigger": [
    "问题询问当前状态、过去状态或特定时间状态",
    "候选记忆中同一实体属性存在多个版本"
  ],
  "failure_signature": [
    "旧事实与新事实同时被返回",
    "系统仅按语义相似度选择旧事实",
    "未识别问题中的时间限定"
  ],
  "operator_plan": [
    {
      "operator": "FORMULATE",
      "instruction": "识别目标实体、属性和目标时间"
    },
    {
      "operator": "FILTER",
      "instruction": "排除在目标时间无效的事实"
    },
    {
      "operator": "RERANK",
      "instruction": "优先排序时间有效且来源可靠的候选"
    },
    {
      "operator": "VERIFY",
      "instruction": "检查最终证据是否仍含未解释冲突"
    }
  ],
  "invariants": [
    "不得把当前无效等同于历史上从未成立",
    "不得删除问题所要求的历史事实"
  ],
  "success_criteria": [
    "返回事实在目标时间有效",
    "证据中不存在未解释的同属性冲突"
  ],
  "retrieval_text": "名称、摘要、触发条件和故障特征的规范化文本",
  "status": "candidate",
  "version": 1,
  "parent_versions": [],
  "source_repairs": [],
  "validation_summary": {},
  "created_at": "...",
  "updated_at": "..."
}
```

### 11.1 兼容最小实现

在原型阶段，可以继续只保存：

```json
{
  "skill_name": "...",
  "skill_abstract": "...",
  "action": ["...", "..."]
}
```

但应在逻辑层要求 `action` 至少覆盖：

- 何时适用；
- 故障如何识别；
- 需要调用哪些抽象操作；
- 有哪些不可破坏的约束；
- 如何判断修复成功。

---

## 12. 两类 Skill Bank

系统维护两套相互独立、结构一致的技能库：

```text
Construction Skill Bank
    → 供 Memory Construction Agent 使用

Access Skill Bank
    → 供 Memory Access Agent 使用
```

两套 Skill Bank 初始均为空，只预先定义：

- Skill Schema；
- Skill 检索工具；
- Skill 增删改并工具；
- 状态转换规则；
- 版本和来源追踪；
- 最大修复迭代次数；
- 验证和回归门槛。

不预置任何任务特定 Skill，以避免把人工先验误认为学习结果。

---

# 第五部分：记忆故障归因

## 13. 错误输入与事实分解

故障归因器（Memory Failure Attributor）接收：

```text
question / task request
reference answer or feedback
model output
normal access queries
normal access results
construction traces
access traces
skills retrieved / selected / executed
memory checkpoints
```

归因器可以使用以下诊断工具：

```text
高召回搜索完整或受限 MemDB
读取具体记忆条目
搜索原始交互历史
读取相关 session
读取构建前后的 memory checkpoint
比较 memory diff
重放指定构建步骤
对候选事实进行 LLM 语义核验
```

首先判断任务输出：

```text
correct
partially_correct
incorrect
invalid_or_ambiguous
```

对部分正确或错误答案，将参考答案拆分为若干必要事实或必要条件，逐事实归因。一个问题可以同时包含多个故障类型。

---

## 14. 四类故障

### 14.1 Construction Failure：记忆构建故障

定义：

> 目标信息存在于原始交互中，但当前记忆状态中不存在、内容不正确、结构不完整，或在后续更新中被错误覆盖、合并、删除或失效。

常见子类型：

- 信息识别遗漏；
- 事实抽取错误；
- 首次未写入；
- 实体或属性归属错误；
- 否定、条件或数值丢失；
- 更新未生效；
- 新旧事实错误覆盖；
- 重复事实错误合并；
- 应失效的事实仍保持有效；
- 应保留的历史或例外信息被删除。

构建故障任务保存：

```text
gold / corrected fact
raw interaction evidence
memory checkpoint before failure
memory checkpoint after failure
memory diff
related memory entries
construction operations and trace
construction skills retrieved / selected / executed
```

修复目标：

> 让原始交互中的目标信息被正确构建为记忆，并在后续更新中满足忠实性、完整性、时间一致性和历史保持要求。

---

### 14.2 Access Failure：记忆访问故障

定义：

> 目标事实已经存在于当前记忆状态中，但正常访问策略未能返回足以完成任务的正确证据。

常见子类型：

- 查询构造遗漏实体、属性或时间；
- 同义词、别名或表达差异导致召回失败；
- top-k 截断；
- 元数据或时间过滤错误；
- 候选重排错误；
- 新旧版本选择错误；
- 多跳访问或关联扩展失败；
- 多条证据未聚合；
- 冲突证据未核验。

归因器对 MemDB 执行诊断性高召回访问，包括：

```text
多组语义查询
关键词查询
实体查询
时间条件查询
邻域或多跳查询
全量候选重排
LLM 语义核验
```

若目标事实可以通过诊断访问找到，但没有进入正常访问证据，则生成 Access Failure Task。

访问故障任务保存：

```text
question
gold / corrected fact
normal access queries
normal access results
diagnostic / oracle queries
diagnostic memory entries
access skills retrieved / selected / executed
access execution trace
```

修复目标：

> 在不修改当前记忆状态的前提下，使正常访问策略能够稳定返回当前 MemDB 中已经存在的目标证据。

---

### 14.3 Utilization Failure：记忆利用故障

定义：

> 正确且充分的目标证据已经被访问策略返回，但任务模型没有正确使用证据，或发生了独立于记忆的推理错误。

此类错误包括：

- 忽略已召回证据；
- 证据到答案的逻辑推理错误；
- 格式化或计算错误；
- 任务指令遵循错误。

Utilization Failure 不进入 Construction/Access Skill Bank。可单独记录，用于回答模型优化或作为“非记忆故障拒修率”指标。

---

### 14.4 Invalid / Unanswerable

包括：

- 原始历史中没有目标事实；
- 参考答案错误；
- 问题本身歧义；
- 反馈不足以判断正确性；
- 无法可靠归因。

此类样本不触发记忆 Skill 更新。

---

## 15. 故障归因优先级

建议对每个必要事实按以下顺序诊断：

1. 检查正常访问结果中是否已有充分证据；
   - 若有，优先判定 Utilization Failure；
2. 检查当前 MemDB 中是否存在目标事实；
   - 若存在但正常访问未返回，判定 Access Failure；
3. 检查原始交互中是否存在目标事实；
   - 若存在但 MemDB 中缺失或错误，判定 Construction Failure；
4. 若原始交互中不存在或证据不足，判定 Invalid / Unanswerable。

一个问题的不同必要事实可以分别属于不同类型。

---

# 第六部分：Skill 的生成、检索与演化

## 16. 先独立起草，再检索 Skill Bank

处理一个错误任务时，不把整个 Skill Bank 直接塞给 Meta-Agent。保留当前版本中的两阶段机制：

```text
Failure Task
    ↓
Meta-Agent 独立生成 Draft Skill
    ↓
使用 Failure Task + Draft Skill 检索现有 Skill
    ↓
返回 Top-k 候选 Skill
    ↓
Meta-Agent 决定 REUSE / ADD / UPDATE / MERGE / DELETE
```

这样做有三点作用：

1. 防止 Meta-Agent 过早锚定现有 Skill；
2. 让 Draft Skill 成为检索查询，提升抽象相似技能的召回；
3. 能比较“从当前错误独立归纳出的策略”与“已有知识资产”之间的关系。

必须区分：

- **Draft Skill**：临时推理产物，尚未进入技能库；
- **Candidate Skill**：经过复用、添加、更新或合并后形成，进入验证阶段的新版本。

---

## 17. Skill 检索

Meta-Agent 可调用：

```text
skill.search(queries, top_k, skill_type)
skill.get(skill_id_or_name)
skill.trace(skill_id_or_name)
skill.compare(skill_ids)
```

### 17.1 检索查询

使用两类查询：

**任务查询**

```text
当前错误
故障证据
失败轨迹
根因类型
修复目标
基座能力约束
```

**Draft 查询**

```text
draft skill name
draft abstract
draft trigger
draft failure signature
draft operator plan
```

### 17.2 混合检索

```text
Embedding Retrieval
+
BM25 / Keyword Retrieval
+
结构字段匹配
+
故障类型和 Skill 类型过滤
+
可选的 LLM Candidate Verification
```

粗召回优先使用：

```text
skill_name
skill_abstract
trigger
failure_signature
```

只有当 Meta-Agent 判断某个 Skill 可能相关时，再读取完整操作计划、不变量和版本轨迹。

---

## 18. Meta-Agent 的技能操作

Meta-Agent 可以选择：

```text
REUSE
ADD
UPDATE
MERGE
DELETE
```

### REUSE

现有 Skill 已经足以覆盖当前错误。需要进一步判断之前失败来自：

- Skill 未被检索；
- Skill 被检索但未被选择；
- Skill 被选择但未执行；
- Skill 执行过程不符合要求。

### ADD

现有 Skill 无法覆盖当前故障模式，使用 Draft 创建新的 Candidate。

### UPDATE

某个现有 Skill 的抽象方向正确，但存在以下问题：

- 触发条件不完整；
- 故障特征描述不充分；
- 操作步骤缺失；
- 不变量或成功判据不足；
- 摘要导致检索困难。

每次 UPDATE 生成新版本，不直接覆盖旧版本。

### MERGE

多个 Skill 明显重复、互补或分别覆盖同一故障的不同阶段时，生成一个新的合并版本。

MERGE 必须记录：

- 来源 Skill；
- 保留的触发条件；
- 删除的重复动作；
- 新增的统一不变量；
- 旧 Skill 到新 Skill 的映射。

### DELETE

只用于持续错误、完全重复、已被稳定替代或明显泄漏案例信息的 Skill。删除操作默认转入冻结状态，物理删除只用于隐私和数据治理需求。

---

## 19. Skill 状态与版本

建议使用四种状态：

```text
candidate
use
quarantined
frozen
```

### candidate

正在验证的新 Skill 或新版本。可在定向回放中执行，但不默认进入全局在线检索。

### use

已通过目标修复、自然调用和回归测试，可以在正常运行中检索和执行。

### quarantined

在当前案例上可能有用，但验证不足、存在回归或达到最大迭代次数仍未稳定。保留完整轨迹，不参与正常运行。

### frozen

旧版本、合并来源版本、明确失败版本或待删除版本。不参与正常检索，但允许 Meta-Agent 查看历史。

### 19.1 版本规则

每次修改生成新版本：

```text
S0: use
   ↓ UPDATE trigger / abstract
S1: candidate
   ↓ 强制调用成功，自然调用失败
S1: quarantined
   ↓ 修改 retrieval fields
S2: candidate
   ↓ 全部验证通过
S2: use
S0: frozen
```

版本记录至少包括：

- parent version；
- 操作类型；
- Meta-Agent 决策依据；
- 来源错误任务；
- 强制调用结果；
- 自然调用结果；
- 局部和全局回归结果；
- 最终状态。

---

# 第七部分：单任务修复流程

## 20. Preflight

错误池中的任务可能因为其他任务更新了共享 Skill Bank 而自动被修复。因此正式处理前，先使用当前 Skill Bank 轻量重跑：

### 已经修复

```text
标记 auto_resolved
记录实际起作用的 Skill
不再进入 Meta-Agent
```

### 仍然失败

```text
重新获取当前 Skill Bank 状态
重新检索相关 Skill
再进入 Meta-Agent
```

错误任务只绑定稳定的失败证据，不永久绑定某个 Skill 版本。

---

## 21. Candidate 的四层验证

当前版本中的“强制调用 + 自然调用”应保留，并扩展为四层验证。

### 21.1 层一：操作计划静态检查

检查 Candidate 是否：

- 属于正确 Skill 类型；
- 使用合法抽象操作；
- 不包含具体用户、问题或参考答案泄漏；
- 不违反基座能力约束；
- 含有明确成功判据；
- 不存在内部矛盾。

### 21.2 层二：强制调用测试

直接把 Candidate 提供给对应的构建或访问智能体，绕过自然技能检索。

目的：

> 判断 Skill 内容本身是否能够修复目标错误。

记录：

```text
compiled
executed
target_fact_recovered
target_answer_fixed
trace_compliance
```

若强制调用失败，优先修改操作计划、不变量或故障归因。

### 21.3 层三：自然调用测试

恢复正常 Skill 检索与选择机制。

目的：

> 判断 Skill 是否能够在真实运行中被检索、选择、编译、执行并产生效果。

记录完整链路：

```text
retrieved
selected
compiled
executed
effective
```

失败解释：

| 阶段 | 可能原因 | 优先修复对象 |
|---|---|---|
| 未检索 | 摘要、触发条件或检索器问题 | retrieval_text / trigger / 检索策略 |
| 已检索未选择 | 选择器判断错误或技能冲突 | selection policy / 技能边界 |
| 已选择未编译 | 适配器不支持或操作集不匹配 | adapter / operator plan |
| 已编译未执行 | 工具调用或流程执行问题 | execution layer |
| 已执行无效 | Skill 内容或根因判断错误 | operator plan / attribution |

### 21.4 层四：回归测试

Candidate 不仅要修复当前错误，还要通过：

1. **局部邻域回归集**：相似用户状态、相似问题或同一记忆分支；
2. **历史受益集**：此前被相同 Skill 修复的任务；
3. **全局抽样回归集**：其他类型任务和随机历史；
4. **对抗边界集**：与触发条件相似但不应调用该 Skill 的案例。

上线条件建议为：

```text
target repair = success
natural invocation = success
local regression degradation <= threshold_local
global regression degradation <= threshold_global
leakage check = pass
```

未满足时进入 `quarantined`，不得直接转为 `use`。

---

## 22. 最大迭代次数

达到最大迭代次数仍未完全修复时：

```text
保留最新 Candidate
Candidate → quarantined
标记 unresolved
保存完整 repair trace
```

不得自动将未解决 Candidate 转为 `use`。

后续相似错误可以基于：

```text
当前 use Skill
quarantined Candidate
历史 repair trace
新的失败证据
```

继续优化。

---

## 23. 单个错误任务的完整流程

```text
取出 Failure Task
        ↓
Preflight：使用当前 Skill Bank 重跑
        ├── 已修复 → auto_resolved
        └── 仍失败
                ↓
故障归因复核：Construction / Access / Utilization / Invalid
                ↓
对应 Meta-Agent 独立生成 Draft Skill
                ↓
任务查询 + Draft 查询
                ↓
检索 Top-k 现有 Skill
                ↓
REUSE / ADD / UPDATE / MERGE / DELETE
                ↓
生成 Candidate Version
                ↓
静态检查
                ↓
强制调用测试
                ↓
自然调用测试
                ↓
局部与全局回归测试
                ↓
通过 → use
失败但有价值 → quarantined
明确无效或被替代 → frozen
```

---

# 第八部分：错误池、并发修改与 Epoch

## 24. Error Pool

所有错误任务进入统一错误池，但按故障类型分区：

```text
Construction Failure Pool
Access Failure Pool
Utilization Failure Pool
Invalid / Unanswerable Pool
```

一个问题可同时产生多个事实级任务。例如：

- 一个事实已存在但未被访问；
- 另一个事实从未被正确构建；
- 最终回答还包含额外推理错误。

每个错误任务相互独立，但 Construction/Access 任务分别共享对应 Skill Bank。

---

## 25. Skill 修改冲突

共享 Skill Bank 更新后，其他任务可能：

- 已自动修复；
- 仍然失败，但相关 Skill 已更新；
- 原来引用的 Skill 已被合并或冻结；
- 新 Skill 修复当前任务但造成新的回归。

处理原则：

```text
错误任务保存稳定失败证据
Skill Manager 保存所有版本和映射
任务处理前执行 Preflight
验证时读取当前技能依赖关系
完整轨迹按需读取
```

合并映射示例：

```text
Old Skill A → New Skill C
Old Skill B → New Skill C
```

若某 Skill 被冻结或删除，旧引用触发重新检索。

---

## 26. 一个 Epoch 的处理流程

### 26.1 Epoch 开始

固定当前版本：

```text
Memory State / MemDB
Construction Skill Bank
Access Skill Bank
Base Model and Adapter Versions
```

运行全部训练或开发 QA，统一收集任务输出和轨迹。

### 26.2 统一归因

故障归因器生成：

```text
Access Failure Pool
Construction Failure Pool
Utilization Failure Pool
Invalid Pool
```

### 26.3 Phase A：处理 Access Failure

```text
固定当前 MemDB
逐个修复 Access Skill
每次修改前执行 Preflight
通过验证后更新 Access Skill Bank
```

优先处理 Access Failure 的原因是：在不改变记忆状态的情况下，先判断现有记忆能够支持到什么程度，避免把纯访问问题错误归因到构建阶段。

### 26.4 Phase B：处理 Construction Failure

```text
逐个修复 Construction Skill
从故障前 checkpoint 开始局部重放
更新对应 MemDB 分支
执行构建和访问联合验证
```

Construction 修复后会改变 MemDB，并可能产生新的访问模式或访问错误，因此跨类型影响放到下一 Epoch 统一重新发现。

### 26.5 Epoch 结束

保存：

```text
New Construction Skill Bank
New Access Skill Bank
New Memory State Version
Repair Traces
QA Logs
Auto-resolved Tasks
Quarantined Candidates
Unresolved Tasks
Regression Reports
```

下一 Epoch 使用新版本重新运行全部 QA。

---

# 第九部分：实现模块与工具接口

## 27. 模块命名

| 当前名称 | 新版建议名称 | 作用 |
|---|---|---|
| Memory Writer Agent | Memory Construction Agent | 执行记忆构建策略 |
| Memory Reader Agent | Memory Access Agent | 执行记忆访问策略 |
| Judge-Agent | Memory Failure Attributor | 判断正确性并定位根因 |
| Write Meta-Agent | Construction Meta-Agent | 生成和演化构建 Skill |
| Read Meta-Agent | Access Meta-Agent | 生成和演化访问 Skill |
| Write Skill Bank | Construction Skill Bank | 保存构建程序性元记忆 |
| Read Skill Bank | Access Skill Bank | 保存访问程序性元记忆 |
| Skill Manager | Skill Lifecycle Manager | 检索、版本、状态、合并和验证管理 |
| Base Adapter | Memory System Adapter | 抽象操作到本地操作的映射 |

---

## 28. Skill 工具

### 28.1 读取工具

```text
skill.search(queries, top_k, skill_type, status_filter)
skill.get(skill_id_or_name, version)
skill.trace(skill_id_or_name)
skill.compare(skill_ids_or_versions)
skill.dependencies(skill_id_or_name)
```

### 28.2 修改工具

```text
skill.add(skill_object)

skill.update_fields(
  skill_id,
  expected_version,
  field_patch
)

skill.merge(
  source_skill_ids,
  merged_skill_object
)

skill.freeze(skill_id, reason)
skill.quarantine(skill_id, reason)
skill.delete(skill_id, reason)
```

状态切换由 Skill Lifecycle Manager 根据验证结果自动完成，Meta-Agent 只提出操作建议和 Candidate 内容。

---

## 29. 诊断工具

```text
memory.search_diagnostic(queries, filters, high_recall=true)
memory.get(entry_id)
memory.snapshot(checkpoint_id)
memory.diff(before_checkpoint, after_checkpoint)
memory.restore(checkpoint_id)
conversation.search(query, session_range)
conversation.get(session_id)
trace.get_construction(trace_id)
trace.get_access(trace_id)
replay.construction(checkpoint, interaction, skills)
replay.access(memory_state, question, skills)
```

---

## 30. 必须记录的运行轨迹

### 30.1 构建轨迹

```text
interaction id
input interaction
memory snapshot before
candidate facts
selected construction skills
abstract operators
compiled local operations
tool calls
memory diff
memory snapshot after
verification result
```

### 30.2 访问轨迹

```text
question id
question
selected access skills
abstract operators
compiled queries
raw candidates
filters
reranking scores
final evidence
verification result
```

### 30.3 修复轨迹

```text
failure task
diagnostic evidence
attributed failure type
draft skill
retrieved existing skills
meta-agent decision
candidate versions
forced invocation results
natural invocation results
regression results
final state
```

---

# 第十部分：基座选择与实验总体思路

## 31. 基座选择原则

论文不应在“原生适配、经典工业方案、新强方案”中三选一。三类基座承担不同研究功能。

### 31.1 原生透明基座：机制验证

目标：提供完全可观测、可注入故障、可保存检查点、可局部重放的研究仪器。

建议组件：

- 大模型事实抽取；
- 简单的 ADD / REVISE / INVALIDATE / MERGE；
- 向量检索；
- 可选关键词检索；
- 简单过滤和重排；
- 固定回答模型。

原生基座不追求复杂和最强性能，主要用于：

- 验证故障归因准确性；
- 精确注入构建和访问故障；
- 分析强制调用与自然调用；
- 完成详细消融；
- 检查 Skill 的操作级行为。

原生基座不能成为唯一实验对象，否则容易被质疑为自设靶子。

### 31.2 经典工业基座：正文主要运行实例

选择一个接口成熟、应用广泛、具备明确记忆增删改查流程的工业记忆框架作为主要实例。

其作用是：

- 证明 MiM 能提升真实、稳定、非自建系统；
- 作为正文中的贯穿示例；
- 展示 Construction/Access Skill 如何映射为提示词、工具和检索策略；
- 测量工程成本、延迟和兼容性。

正文方法描述仍使用抽象系统，工业基座只作为实例，不作为 MiM 定义的一部分。

### 31.3 新强基座：强基座与结构泛化

选择一种具有更复杂记忆组织或访问机制的新强方法，例如：

- 动态链接或演化式记忆；
- 图结构或时间知识图谱；
- 多阶段记忆管理；
- 学习型记忆控制器。

其作用是：

- 验证 MiM 在较强基座上仍有增益；
- 证明 Skill 不只适用于简单向量检索；
- 测试抽象操作能否映射到异构存储；
- 避免被认为只是为某个工业框架补充提示词。

### 31.4 推荐的最小组合

```text
Base A：原生透明向量记忆
Base B：经典工业记忆框架
Base C：新强结构化或图式记忆框架
```

资源有限时至少完成 A+B；具有完整论文说服力的配置为 A+B+C。

---

# 第十一部分：实验问题

## 32. 研究问题

### RQ1：端到端有效性

MiM 是否能够提高不同基座在长期记忆任务上的最终准确率，并降低重复错误？

### RQ2：故障归因有效性

MiM 能否准确区分构建故障、访问故障、利用故障和无效样本？

### RQ3：修复有效性

MiM 生成的 Skill 是否真正修复目标错误，而不是依靠参考答案泄漏或特例记忆？

### RQ4：自然调用有效性

在不强制提供 Skill 的情况下，系统能否正确检索、选择、编译和执行该 Skill？

### RQ5：抽象与复用

一个错误产生的 Skill 能否自动修复其他实体、用户、表达或数据集中的相似错误？

### RQ6：跨基座可插拔性

冻结抽象 Skill Bank、仅替换适配器后，Skill 是否仍能迁移到新的记忆基座？

### RQ7：持续演化稳定性

技能库随 Epoch 增长时，性能是否持续提升，技能数量是否可控，是否出现回归、冲突和冗余？

### RQ8：成本收益

MiM 带来的额外大模型调用、延迟和存储成本，是否能被性能增益和错误减少抵消？

---

# 第十二部分：数据集与任务设计

## 33. 真实长期记忆基准

建议至少覆盖以下能力：

- 单事实回忆；
- 跨会话信息整合；
- 时间推理；
- 状态更新；
- 多跳关联；
- 选择性遗忘或失效；
- 无答案拒答。

主实验可选择两个公开长期记忆基准：

1. 一个强调长周期对话、跨会话和时间问题的基准；
2. 一个强调增量交互、状态更新和长期记忆能力分解的基准。

为避免数据集名称变化影响方案，本技术文档不绑定单一基准，但执行阶段优先使用能够同时评估构建和访问的公开数据集。

---

## 34. 可控故障注入集

必须自建一组可控故障注入集，用于评估根因归因，而不仅是最终 QA。

### 34.1 构建故障注入

- 关键信息未写入；
- 实体归属错误；
- 属性字段错误；
- 数值、日期或单位错误；
- 否定词丢失；
- 条件或例外丢失；
- 新状态未更新；
- 新事实错误覆盖历史事实；
- 多条事实错误合并；
- 旧事实未失效；
- 仍有效信息被错误删除；
- 更新时间或有效区间错误。

### 34.2 访问故障注入

- 查询未包含核心实体；
- 别名或同义表达未扩展；
- top-k 过小；
- 时间过滤方向错误；
- 元数据过滤过严或过松；
- 重排器选择旧版本；
- 多跳关系未遍历；
- 互补证据未聚合；
- 冲突候选未核验；
- 已召回正确事实但在后处理阶段被丢弃。

### 34.3 非记忆故障注入

- 正确证据已返回但回答模型推理错误；
- 计算或格式错误；
- 原始历史无答案；
- 参考答案错误；
- 问题歧义。

可控故障集应提供确定的根因标签、目标修复点和期望操作，以测量归因 F1 和操作级修复率。

---

# 第十三部分：基线设计

## 35. 基座和经验学习基线

每个基座至少比较：

1. **Base Only**：原始基座；
2. **Base + More Retrieval**：简单增加召回范围或 top-k；
3. **Base + Failure Case Retrieval**：直接保存并检索相似失败案例；
4. **Base + Global Reflection Prompt**：把所有错误总结为一个持续增长的全局构建/访问提示词；
5. **Base + Single Skill Bank**：构建与访问共用一个 Skill Bank；
6. **Base + Static Human Rules**：人工定义少量通用规则；
7. **Base + MiM**：完整方法。

### 35.1 为什么必须加入失败案例检索

它用于证明：

> MiM 的提升不是因为系统额外看到了历史错误，而是因为错误被抽象成可复用程序性元记忆。

### 35.2 为什么必须加入全局提示词优化

它用于证明：

> 结构化、可检索、可版本化的技能库比单一持续膨胀的系统提示词更稳定、更精确。

### 35.3 直接相关方法

将以下类型工作作为方法级对照：

- 从困难案例或奖励中学习记忆技能的方法；
- 使用强化学习训练记忆管理器的方法；
- 从成功/失败轨迹提炼通用经验的方法；
- 自动优化 Agent 提示词或程序的方法。

比较时必须突出 MiM 的差异：

- 使用下游错误进行显式根因归因；
- 同时修复构建和访问；
- Skill 是外置、可审计、可版本化的程序性元记忆；
- 支持强制/自然调用分离验证；
- 以抽象操作和适配器实现跨基座接入。

---

# 第十三部分补充：实验矩阵与论文表格设计

## 35.4 推荐数据集组合

建议采用“两个真实基准 + 一个可控故障集”的最小组合：

| 数据类型 | 推荐候选 | 主要作用 |
|---|---|---|
| 长期对话记忆 | LoCoMo | 覆盖跨会话事实、时间、多跳和开放式问题，便于与现有记忆方法对齐 |
| 记忆能力分解 | LongMemEval | 覆盖检索、跨会话、时间、状态更新和拒答，适合构建—访问分解 |
| 增量智能体记忆，可选 | MemoryAgentBench | 评估增量学习、长期理解和选择性遗忘等能力 |
| 长轨迹扩展，可选 | LongMemEval-V2 或同类轨迹基准 | 测试工作流知识、动态状态和环境经验记忆 |
| 自建诊断集 | MiM-FaultBench（暂定名） | 提供确定根因标签，用于归因、修复和操作级评测 |

数据划分除标准训练/开发/测试外，还应构造：

```text
Entity-disjoint split
User-disjoint split
Question-template-disjoint split
Failure-instance-disjoint split
Cross-dataset split
Cross-base split
```

---

## 35.5 分阶段实验矩阵

| 实验阶段 | 基座 | 数据 | 主要变量 | 主要回答的问题 |
|---|---|---|---|---|
| E1 机制验证 | 原生透明基座 | MiM-FaultBench | 有无归因、Skill 结构、验证门 | 是否能正确定位并修复故障 |
| E2 端到端主实验 | 原生 + 工业基座 | LongMemEval、LoCoMo | Base、案例检索、全局提示词、MiM | 是否带来稳定端到端提升 |
| E3 强基座实验 | 新强基座 | LongMemEval、LoCoMo | Base vs Base+MiM | 强基座上是否仍有增益 |
| E4 Skill 复用 | 原生/工业基座 | 实体和模板隔离测试 | 训练错误数、技能库规模 | 是否学到抽象而非案例记忆 |
| E5 跨数据集 | 同一基座 | A 训练、B 测试 | 冻结 Skill Bank | 程序性元记忆能否跨任务迁移 |
| E6 跨基座 | A 学习、B 执行 | 同一测试集 | 只替换 Adapter | 可插拔性是否真实成立 |
| E7 持续学习 | 至少两个基座 | 多 Epoch | Epoch 数、任务顺序 | 技能演化是否稳定且无灾难回归 |
| E8 成本分析 | 所有基座 | 主测试集 | 调用数、令牌、延迟 | 性能收益是否值得额外成本 |

---

## 35.6 建议论文主表

### 主表 1：不同基座上的端到端结果

| Method | Native Base | Industrial Base | Strong Base | Average |
|---|---:|---:|---:|---:|
| Base Only |  |  |  |  |
| + More Retrieval |  |  |  |  |
| + Failure Case Retrieval |  |  |  |  |
| + Global Reflection Prompt |  |  |  |  |
| + Single Skill Bank |  |  |  |  |
| + MiM |  |  |  |  |

按问题类型进一步拆分：

```text
single-hop
multi-hop
temporal
knowledge update
conflict resolution
abstention
```

### 主表 2：故障归因与修复

| Method | Attribution Macro-F1 | Construction Repair | Access Repair | Non-memory Reject | Regression Rate |
|---|---:|---:|---:|---:|---:|
| Direct Reflection |  |  |  |  |  |
| No Trace |  |  |  |  |  |
| No Memory Checkpoint |  |  |  |  |  |
| Full MiM |  |  |  |  |  |

### 主表 3：自然调用链

| Setting | Retrieval Recall@k | Selection Precision | Compile Success | Execution Compliance | Effective Invocation |
|---|---:|---:|---:|---:|---:|
| Free-form Skill |  |  |  |  |  |
| Structured Skill |  |  |  |  |  |
| Structured Skill + Operator Set |  |  |  |  |  |
| Full MiM + Regression Gate |  |  |  |  |  |

### 主表 4：跨基座迁移

| Source → Target | Target Base Only | Target + Bound Actions | Target + Abstract Skills | Full Retraining | Gain Retention |
|---|---:|---:|---:|---:|---:|
| Native → Industrial |  |  |  |  |  |
| Native → Strong |  |  |  |  |  |
| Industrial → Strong |  |  |  |  |  |

### 主表 5：技能库演化

| Epoch | QA Score | Skill Count | New | Updated | Merged | Auto-resolved | Quarantined | Global Regression |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 0 |  | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| 1 |  |  |  |  |  |  |  |  |
| 2 |  |  |  |  |  |  |  |  |
| ... |  |  |  |  |  |  |  |  |

---

## 35.7 关键统计规范

- 所有端到端结果至少运行 3 个随机种子；
- 对同一问题采用配对显著性检验或自助法置信区间；
- 报告均值、标准差和 95% 置信区间；
- 主表固定回答模型和大模型版本，避免基座间模型差异掩盖方法效应；
- 记录并固定检索 top-k、上下文预算和最大工具调用数；
- 报告失败任务选择顺序，并增加至少一种随机顺序复验；
- 对人工评估的 Skill 抽象性、忠实性和泄漏性报告评审者一致性。

---

# 第十四部分：评价指标

## 36. 端到端指标

- 最终 QA 准确率；
- F1 / Exact Match；
- 多跳问题准确率；
- 时间问题准确率；
- 状态更新问题准确率；
- 拒答准确率；
- 每 Epoch 性能曲线；
- 重复错误率。

---

## 37. 故障归因指标

- Construction / Access / Utilization / Invalid 宏平均 F1；
- 事实级根因定位准确率；
- 非记忆故障拒修率；
- 构建子类型准确率；
- 访问子类型准确率；
- Oracle 诊断召回率；
- 错误归因置信校准。

---

## 38. 修复与调用链指标

### 38.1 修复指标

- Target Repair Rate：目标错误修复率；
- First-attempt Repair Rate：首次 Candidate 修复率；
- Average Repair Iterations：平均修复轮数；
- Local Regression Rate：局部回归率；
- Global Regression Rate：全局回归率；
- Quarantine Rate：隔离比例；
- Unresolved Rate：未解决比例。

### 38.2 自然调用指标

- Skill Retrieval Recall@k；
- Selection Precision；
- Compilation Success Rate；
- Execution Compliance；
- Effective Invocation Rate；
- End-to-end Skill Utilization Rate。

---

## 39. Skill 质量指标

- Skill Reuse Count：单 Skill 被复用次数；
- Auto-resolved Rate：被其他任务产生的 Skill 自动修复的比例；
- Coverage：一个 Skill 覆盖的独立错误数；
- Redundancy：技能库冗余度；
- Merge Rate：技能合并率；
- Skill Growth：技能数量随任务增长曲线；
- Trigger Precision：相似但不适用案例上的不触发准确率；
- Leakage Rate：Skill 与具体训练答案或实体的泄漏率。

---

## 40. 泛化与迁移指标

### 40.1 跨实体与跨用户

训练和测试中的用户、实体名称完全隔离。

### 40.2 跨表达

测试问题模板、措辞和表述方式与训练错误隔离。

### 40.3 跨数据集

在数据集 A 学习 Skill，冻结后测试数据集 B。

### 40.4 跨基座

冻结 Skill Bank，仅更换 Adapter：

```text
Train Skills on Base A
Freeze Skill Bank
Build Adapter for Base B
Evaluate on Base B without Skill Update
```

定义跨基座增益保留率：

\[
\mathrm{Retention}_{A\rightarrow B}
=
\frac{\Delta \mathrm{Performance}_{B,\,transferred}}
{\Delta \mathrm{Performance}_{A,\,source}}
\]

还应报告迁移所需的：

- 适配器代码量；
- 人工映射规则数量；
- 无法映射的操作比例；
- 零样本与少样本适配性能。

---

## 41. 成本指标

- 每个正常问题额外 LLM 调用数；
- 每个错误修复额外调用数；
- 平均输入/输出令牌；
- 正常阶段延迟；
- 诊断和修复阶段延迟；
- Skill Bank 存储规模；
- 适配器维护成本；
- 每修复一个错误的平均费用。

---

# 第十五部分：消融实验

## 42. 核心消融

1. 去掉故障归因，直接从错误生成 Skill；
2. 构建与访问共用单一 Skill Bank；
3. 去掉 Draft-first，直接检索现有 Skill；
4. 去掉任务查询，仅使用 Draft 查询；
5. 去掉 Skill 检索，始终新增；
6. 去掉 MERGE，只允许 ADD/UPDATE；
7. 去掉版本与冻结；
8. 去掉 Preflight；
9. 去掉强制调用测试；
10. 去掉自然调用测试；
11. 去掉局部回归；
12. 去掉全局回归；
13. Skill 使用自由自然语言，不使用抽象操作集；
14. Skill 直接绑定基座 API，不使用 Adapter；
15. 用原始错误案例替代抽象 Skill；
16. 用单一全局提示词替代 Skill Bank。

### 42.1 最关键的三个消融

若资源有限，优先完成：

- 无故障归因；
- 单一全局提示词；
- 基座绑定 Skill vs 抽象操作 Skill。

这三组直接决定论文能否证明：MiM 不是一般反思、提示词修补或某个基座的附属组件。

---

# 第十六部分：论文主体行文建议

## 43. 主体叙事

论文不以某个具体基座为方法主语，而采用以下顺序：

1. 定义大模型介导的智能体记忆系统；
2. 将系统分解为记忆构建策略和记忆访问策略；
3. 定义两类抽象操作集；
4. 说明下游错误可以沿交互—记忆—访问—回答链路归因；
5. 提出从修复轨迹中学习程序性元记忆；
6. 通过 Adapter 把 Skill 编译到不同基座；
7. 在多个基座上验证有效性、可插拔性和复用性。

### 43.1 推荐的一句话贡献

> MiM 通过从下游失败轨迹中归纳程序性元记忆，持续修复大模型介导的记忆构建策略和记忆访问策略。

### 43.2 更完整的方法摘要

> MiM 是一个面向大模型介导记忆系统的外置控制层。它联合检查原始交互、记忆状态、构建轨迹和访问轨迹，对下游任务错误进行根因归因，并将经过验证的修复过程抽象为可复用的程序性元记忆。程序性元记忆通过基座无关的抽象操作表示，并由适配器编译到具体记忆系统，从而在不修改基座模型参数的情况下持续改善记忆构建和记忆访问。

---

## 44. 贯穿示例

建议正文始终使用同一个时间状态更新案例：

```text
Session 1：用户说“我目前住在北京。”
Session 8：用户说“我上个月已经搬到上海。”
```

可能的构建故障：

- 没有记录搬迁；
- 把上海错误识别为旅行地点；
- 直接覆盖北京，导致历史事实丢失；
- 保留北京和上海，但未记录有效时间；
- 更新时丢失“上个月”这一时间信息。

可能的访问故障：

- 问当前地址时返回北京；
- 问过去地址时只返回上海；
- 两个地址都返回但没有解决时间冲突；
- 查询没有提取“当前”约束；
- 正确候选因 top-k 或重排被淘汰。

这个案例能够统一解释：

- 构建和访问的边界；
- 操作集；
- 故障归因；
- Construction/Access Skill；
- 强制和自然调用；
- 向量、文档和图基座上的适配差异。

---

# 第十七部分：与当前版本的兼容关系

## 45. 保留不变的核心机制

新版完整保留以下设计：

- 记忆存在但未正确访问、原始交互存在但未正确构建的两类核心问题；
- 两套独立 Skill Bank；
- Judge/Attributor 使用 MemDB、原始交互和 checkpoint 进行诊断；
- 错误任务保存稳定的失败证据；
- Meta-Agent 先独立生成 Draft，再检索现有 Skill；
- Skill 的 REUSE / ADD / UPDATE / MERGE / DELETE；
- Skill 混合检索；
- Skill 版本化而非原地覆盖；
- Preflight 自动解决；
- 强制调用和自然调用双验证；
- retrieved / selected / executed / effective 链路记录；
- 任务不永久绑定 Skill 版本；
- Type/Phase 分阶段处理；
- Construction 修复从 checkpoint 局部重放；
- Epoch 结束统一保存 Skill Bank、MemDB 和 Repair Trace；
- 测试前冻结 Skill Bank，测试阶段不再利用 gold 更新。

---

## 46. 新版需要调整的部分

| 当前设计 | 新版调整 | 原因 |
|---|---|---|
| Writer / Reader Agent | Construction / Access Agent | 覆盖更新、失效、查询规划、过滤和聚合等更完整职责 |
| Type1 / Type2 | Access / Construction Failure | 命名直接对应流水线阶段 |
| Type3 / Invalid 模糊存在 | 正式定义 Utilization / Invalid | 防止将回答模型错误误修为记忆错误 |
| Skill 只有 abstract + action | 增加 trigger、failure signature、operator plan、invariants、success criteria | 支持抽象、编译、检索和验证 |
| Candidate 可参与正常检索 | 默认仅用于定向验证 | 降低共享技能库污染风险 |
| 最大迭代后 Candidate → use | Candidate → quarantined | 未验证技能不得上线 |
| 只做强制/自然测试 | 增加静态检查和回归测试 | 防止特例修复和全局退化 |
| 泛化主要依靠自然语言 action | 增加抽象操作集和 Adapter | 明确可插拔契约和跨基座迁移路径 |

---

# 第十八部分：风险与应对

## 47. 新颖性风险

### 风险

容易被理解为“从失败案例生成记忆 Skill”或“给现有记忆系统增加反思模块”。

### 应对

正文必须突出：

- 记忆流水线根因归因；
- 构建/访问分治；
- 抽象操作级 Skill；
- 外置控制平面；
- 强制/自然调用和回归验证；
- 跨基座冻结迁移。

---

## 48. Gold 泄漏风险

### 风险

Meta-Agent 可能将具体答案、实体、日期或表达写入 Skill，导致测试提升来自答案记忆。

### 应对

- 实体匿名化；
- 日期和数值模板化；
- Skill 与训练答案 n-gram 重叠检查；
- 命名实体和罕见字符串检测；
- 用户和实体级训练测试隔离；
- 测试阶段冻结 Skill Bank；
- 删除来源案例后的反事实复测。

---

## 49. 归因器成为隐藏 Oracle 的风险

### 风险

诊断阶段使用完整 MemDB 和 Gold，可能让系统获得不现实的额外能力。

### 应对

- 明确区分训练/开发诊断与测试运行；
- Gold 只用于离线错误学习；
- 最终测试不提供 Gold、不更新 Skill；
- 增加用户纠正、二元反馈和弱反馈设置；
- 报告 Oracle 诊断工具的调用成本；
- 对比不使用完整 MemDB 的受限诊断版本。

---

## 50. Skill 过度泛化风险

### 风险

Skill 在目标案例上有效，但对边界案例误触发，造成回归。

### 应对

- 明确 Trigger 和 Invariants；
- 增加对抗边界集；
- 报告 Trigger Precision；
- 使用 quarantined 状态；
- 只有通过局部和全局回归才进入 use。

---

## 51. 基座适配工作量风险

### 风险

所谓可插拔可能依赖大量手工代码，使泛化主张失去说服力。

### 应对

- 预先定义最小适配契约；
- 限制抽象操作数量；
- 报告每个基座适配器代码量和人工规则数量；
- 设计统一 Adapter 测试套件；
- 开展冻结 Skill Bank、只替换 Adapter 的迁移实验。

---

# 第十九部分：最终测试协议

## 52. 训练完成后的冻结

冻结：

```text
Final Construction Skill Bank
Final Access Skill Bank
Skill Retrieval Index
Skill Selector
Base Adapters
All Prompts and Model Versions
```

在未见过的历史上：

```text
重新构建 MemDB
使用冻结 Construction Skills 构建记忆
使用冻结 Access Skills 访问记忆
完成官方 QA 或任务
```

测试阶段严格禁止：

```text
向 Construction / Access Agent 提供 gold answer
向 Meta-Agent 提供测试错误
根据测试结果新增或修改 Skill
修改冻结后的 Skill Bank
人工挑选测试时使用的 Skill
```

可以记录测试错误用于事后分析，但不能回写当前测试运行。

---

# 第二十部分：技术路线

## 53. 总体技术路线

```text
阶段 0：统一问题定义和数据协议
    ↓
阶段 1：原生透明基座与全链路日志
    ↓
阶段 2：故障归因器与可控故障集
    ↓
阶段 3：Construction / Access Skill Bank
    ↓
阶段 4：Draft-first、检索、增删改并和版本管理
    ↓
阶段 5：强制调用、自然调用和回归验证
    ↓
阶段 6：Epoch 学习与技能复用分析
    ↓
阶段 7：工业基座 Adapter
    ↓
阶段 8：新强/异构基座 Adapter 与跨基座迁移
    ↓
阶段 9：完整实验、消融、成本与论文材料
```

---

## 54. 阶段 0：统一问题定义和协议

### 目标

完成所有模块共享的数据结构和研究边界。

### 工作项

- 定义 Memory State、Interaction、Question、Evidence；
- 定义 Construction Trace 和 Access Trace；
- 定义四类 Failure Task；
- 定义 Skill Schema；
- 定义抽象操作集；
- 定义 Adapter 接口；
- 定义训练、开发、测试隔离规则；
- 定义日志和随机种子规范。

### 交付物

```text
schemas/
  interaction.schema.json
  memory.schema.json
  construction_trace.schema.json
  access_trace.schema.json
  failure_task.schema.json
  skill.schema.json
  repair_trace.schema.json
```

---

## 55. 阶段 1：原生透明基座

### 目标

建立一个简单、完全可观测、可回放的 LLM 记忆系统。

### 工作项

- 实现 Memory Construction Agent；
- 实现 Memory Access Agent；
- 实现最小 MemDB；
- 实现 checkpoint、diff 和 restore；
- 实现混合检索；
- 实现固定 Task/Answer Model；
- 记录全链路轨迹。

### 验收条件

- 可从历史构建 MemDB；
- 可回答长期记忆 QA；
- 可精确重放任意构建步骤；
- 可查看正常访问与诊断访问差异；
- 可人工注入构建和访问故障。

---

## 56. 阶段 2：故障归因器与故障集

### 目标

可靠地区分 Construction、Access、Utilization 和 Invalid。

### 工作项

- 实现事实级 Gold 分解；
- 实现高召回诊断访问；
- 实现 raw history 定位；
- 实现 memory checkpoint 比较；
- 构建可控故障注入集；
- 建立归因标注和评测脚本。

### 验收指标

- 四类故障宏平均 F1；
- 事实级根因定位准确率；
- 非记忆故障拒修率；
- 各构建/访问子类型混淆矩阵。

---

## 57. 阶段 3：Skill Bank 最小闭环

### 目标

完成从单个失败到单个可验证 Skill 的闭环。

### 工作项

- 实现 Construction Meta-Agent；
- 实现 Access Meta-Agent；
- 实现 Draft Skill 生成；
- 实现 Candidate 创建；
- 实现强制调用；
- 实现 Skill Version Trace；
- 实现 candidate/use/quarantined/frozen。

### 验收条件

- 一个构建故障能够生成 Construction Skill 并通过强制调用修复；
- 一个访问故障能够生成 Access Skill 并通过强制调用修复；
- 未解决 Candidate 不会进入 use。

---

## 58. 阶段 4：技能检索与演化

### 目标

让 Skill 从单案例产物变为共享、可复用、可演化资产。

### 工作项

- 实现 Draft-first 检索；
- 实现 Embedding + BM25 + 字段匹配；
- 实现 REUSE / ADD / UPDATE / MERGE / DELETE；
- 实现旧版本映射；
- 实现 Preflight；
- 实现自动解决统计。

### 验收指标

- Skill Retrieval Recall@k；
- Auto-resolved Rate；
- Skill Reuse Count；
- Skill Growth 与冗余度。

---

## 59. 阶段 5：完整验证门

### 目标

确保 Skill 既有效、可自然调用，又不会造成明显回归。

### 工作项

- 实现静态检查；
- 实现自然调用链追踪；
- 实现局部回归集；
- 实现全局回归集；
- 实现对抗边界集；
- 实现泄漏检测；
- 实现自动状态转换。

### 验收条件

- 可以区分未检索、未选择、未编译、未执行和执行无效；
- 每个 use Skill 均有完整验证报告；
- 回归超阈值时自动进入 quarantined。

---

## 60. 阶段 6：Epoch 学习与真实基准

### 目标

验证共享 Skill Bank 在批量错误和多 Epoch 下的持续学习效果。

### 工作项

- 实现统一错误池；
- 实现 Access → Construction 两阶段处理；
- 实现局部构建重放；
- 接入长期记忆基准；
- 运行多 Epoch 学习；
- 分析技能数量、复用、合并和回归。

### 主要输出

- 端到端性能曲线；
- 每 Epoch 自动解决率；
- Skill Bank 演化图；
- 错误类型迁移和交叉影响。

---

## 61. 阶段 7：经典工业基座适配

### 目标

证明 MiM 不是原生基座上的自设改进。

### 工作项

- 实现工业基座 Construction Adapter；
- 实现工业基座 Access Adapter；
- 对齐构建和访问轨迹；
- 实现受限 diagnostic inspect；
- 复用同一 Skill Schema 和 Meta-Agent；
- 报告适配器代码量和人工规则数。

### 关键实验

- Base vs Base+MiM；
- 全局提示词 vs Skill Bank；
- 原始错误检索 vs 抽象 Skill；
- 原生基座 Skill 向工业基座迁移。

---

## 62. 阶段 8：新强/异构基座与跨基座迁移

### 目标

验证抽象操作在更强、不同存储结构上的可迁移性。

### 工作项

- 选择动态图式、时间图式或复杂演化式记忆基座；
- 实现最小 Adapter；
- 冻结源基座 Skill Bank；
- 仅替换 Adapter 执行零样本迁移；
- 增加少样本 Adapter 校准；
- 比较基座绑定 action 与抽象 operator plan。

### 核心指标

- 跨基座增益保留率；
- 无法映射的操作比例；
- 适配人工成本；
- 零样本/少样本迁移性能。

---

## 63. 阶段 9：论文级实验与交付

### 目标

形成完整、可复现且能够支撑核心主张的实验材料。

### 工作项

- 三类基座主表；
- 两个真实基准；
- 可控故障归因实验；
- 完整消融；
- 跨实体、跨数据集、跨基座迁移；
- 运行成本和延迟；
- 人工 Skill 质量评估；
- 典型成功和失败案例；
- 可视化 Skill 演化、调用链和回归。

### 最终交付物

```text
代码：MiM 核心、原生基座、Adapters、评测脚本
数据：故障注入集、训练/开发/测试划分
资产：冻结 Skill Banks、Repair Traces、Regression Reports
论文：方法、理论定位、主实验、消融、案例和限制
文档：Adapter 开发指南、Skill Schema、复现实验说明
```

---

# 第二十一部分：最小可行研究版本

## 64. 最小可行系统（MVP）

资源有限时，优先完成以下闭环：

### 基座

- 一个原生透明向量记忆；
- 一个经典工业记忆框架。

### 故障类型

- Construction：未写入、错误覆盖、时间失效错误；
- Access：查询表达不匹配、时间过滤错误、重排错误；
- Utilization：正确证据已召回但回答错误。

### Skill

- Construction Skill Bank；
- Access Skill Bank；
- Draft-first；
- ADD / UPDATE / MERGE / REUSE；
- candidate/use/quarantined/frozen；
- 强制调用、自然调用、局部回归。

### 实验

- 一个真实长期记忆基准；
- 一个可控故障注入集；
- Base、失败案例检索、全局提示词、MiM；
- 原生和工业基座；
- 跨实体泛化；
- 初步跨基座迁移。

该版本已经能够验证 MiM 的核心论点：

> 错误可以被归因到记忆构建或访问阶段；成功修复可以被抽象为程序性元记忆；该元记忆能够被共享、自然调用并迁移到非原生基座。

---

# 第二十二部分：最终结论

MiM 的最稳妥技术定义不是“一个会进化的记忆 Skill 插件”，而是：

> **一个面向大模型介导记忆系统的错误驱动程序性元记忆控制层。**

其完整作用链为：

```text
下游错误
   ↓
记忆故障归因
   ↓
构建故障 / 访问故障
   ↓
从修复轨迹生成程序性元记忆 Skill
   ↓
Skill 控制 Construction / Access Agent
   ↓
Agent 选择并组合抽象操作
   ↓
Adapter 将抽象操作映射为基座本地操作
   ↓
目标错误修复、相似错误复用、跨基座迁移
```

方法层面以抽象记忆系统为主体，实验层面使用原生透明基座、经典工业基座和新强异构基座分别承担机制验证、现实说服力和泛化验证。这样既与当前实现方案保持连续，又能够形成清晰、可防守且有独立性的论文叙事。

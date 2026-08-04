# Memory in Memory：当前实现与学术表达映射报告

> **文档性质：工程事实报告 + 论文写作转换说明**  
> **用途：回答“系统现在具体实现了什么，以及这些工程机制应如何写成学术方法”**

---

## 1. 为什么需要把实现报告与论文正文分开

当前 MiM 已经具备较完整的工程链路，但论文不能按代码执行顺序逐项介绍。

工程文档关注：

- 哪个模块读取什么文件；
- 哪张表保存什么字段；
- 哪个脚本如何运行；
- 如何恢复、并发和检查错误；
- 哪些参数被配置为多少。

论文正文关注：

- 研究对象是什么；
- 现有方法缺少什么；
- 我们提出了什么新的抽象；
- 该抽象如何被形式化；
- 为什么它能够解决目标问题；
- 哪些实验可以证伪或支持这一主张。

二者之间不能直接复制。工程实现是方法的实例，论文方法是对实现中稳定结构的抽象。

例如，工程上我们做的是：

> 在 Access 表里保存 `skill_trace_json`，其中记录 Top-3 和后五个近邻。

论文中应写成：

> 对每次访问决策，系统记录其实际暴露的程序性策略集合及检索边界附近的反事实候选，
> 从而使后续学习能够区分策略缺失、策略未召回和策略执行无效。

这份报告先陈述当前工程事实，再给出完整的学术化映射。

---

# 第一部分：当前实现报告

## 2. 当前系统边界

当前实现是一个面向 LoCoMo 的 Unified Single-Agent Memory MVP。它包含：

1. 一个可来源追踪、可版本化的记忆构建系统；
2. 一个把检索和回答合并在同一上下文中的 ReAct Agent；
3. 三条权限隔离的错误诊断流程；
4. 两类相互独立的程序性 Skill；
5. Candidate 到 Official Skill Bank 的批量整理和事务发布流程；
6. conversation-level 的训练、验证和测试协议；
7. Token F1 与 C/P/I LLM-as-Judge 两种评价接口。

当前系统不应被描述为：

- 新型向量数据库；
- 通用记忆操作系统；
- 已经验证完成的跨基座框架；
- 已经证明持续多轮收敛的自动优化系统。

当前最准确的工程定位是：

> 一个能够完整观察“对话—构建—版本—检索—回答—诊断—Skill 更新”链路的最小研究基座。

---

## 3. Runtime 实现

### 3.1 Construction Agent

Construction Agent 对每个会话执行两个阶段：

1. 从消息中抽取结构化候选记忆；
2. 读取当前相关记忆，并为全部候选统一规划记忆操作。

候选记忆包含：

- 记忆类型；
- 主体、谓词和对象；
- 独立可读的正文；
- 世界时间；
- 来源消息 ID；
- 实体、关键词、重要性和置信度。

支持的操作为：

```text
ADD
UPDATE
MERGE
DELETE
SKIP
```

操作由模型提出，程序负责约束：

- 只能修改已经暴露的目标记忆；
- 一个目标在同一批次最多被修改一次；
- UPDATE/MERGE 必须生成完整的新正文；
- 非法目标和非法操作被拒绝或确定性降级；
- 整个构建批次以数据库事务提交。

### 3.2 Access & Answer Agent

访问和回答由同一个 Agent 完成。可用工具为：

```text
search_memory
inspect_memory
answer
```

每次工具调用及完整结果都保留在同一消息上下文。模型读取观察后自主决定：

- 证据已经充分，立即回答；
- 信息部分充分，针对缺失事实继续搜索；
- 当前路线无效，改用新查询、新关键词或新检索方式；
- 查看某条记忆的版本或来源。

系统不强制固定搜索次数，只设置最大预算。

回答必须给出实际可见的记忆版本 ID。程序检查引用是否属于本次自然搜索链，避免模型引用
没有看到的证据。

---

## 4. 记忆检索实现

当前检索工具支持：

```text
semantic
bm25
keyword
structured
temporal
hybrid
```

Agent 可以决定：

- query；
- query expansions；
- exact keywords；
- entity filters；
- memory kinds；
- time mode；
- target time；
- 是否读取历史；
- 检索深度；
- top-k。

Hybrid Retrieval 使用四路候选：

```text
Semantic      0.40
BM25          0.30
Keyword       0.15
Structured    0.15
```

通过加权 RRF 融合，再使用实体、时间有效性和当前状态乘数调整。

当前 memory embedding 使用本地 `all-MiniLM-L6-v2`，归一化后以 NumPy 精确点积计算。
该规模下没有引入近似向量数据库。

---

## 5. 记忆存储与来源追踪

系统使用 SQLite 保存：

- conversation、session 和 message；
- construction commit 和输入消息；
- memory candidate 和 construction decision；
- memory version；
- version 与 message 的来源边；
- version 与 parent version 的继承边；
- lineage message；
- change event 和 change parent；
- access run、action、retrieval hit；
- 回答模型实际可见的上下文；
- 最终引用证据；
- QA 与 gold evidence。

SQLite 采用：

- Foreign Keys；
- WAL；
- FTS5；
- 每个 run 独立数据库；
- 构建批次事务；
- 诊断阶段并发只读连接。

记忆同时包含两类时间：

- 世界时间：事实在对话世界中何时有效；
- 系统时间：某个版本在第几个 commit 后成为当前版本。

来源追踪不是只在最终记忆上保存一组 message ID。系统还保存父版本和 change event，因此
能够回答：

```text
某条原始消息第一次形成了哪个候选？
模型当时选择了什么操作？
它生成了哪个记忆版本？
后来哪些 UPDATE/MERGE/DELETE 修改过它？
信息第一次在哪一个版本变化中丢失？
```

---

## 6. Runtime Skill 实现

Access 和 Construction 分别检索自己的 Official Skill。

正式 Skill 的最小结构为：

```json
{
  "name": "...",
  "description": "...",
  "content": ["..."]
}
```

其中：

- `description` 主要用于检索；
- `content` 主要用于指导 Agent。

Access Skill 使用完整问题检索；Construction Skill 使用完整会话检索。

当前排序为：

```text
85% semantic
15% lexical
```

Runtime 默认加载 Top-3，并额外记录后五个未加载近邻。每次记录：

- side；
- query；
- Official Bank version；
- selected Skill snapshots；
- nearby-not-selected Skill snapshots；
- rank 和分数。

Runtime 只能读取 Official Skill Bank，不能读取 Candidate。

---

## 7. Diagnosis V3 实现

### 7.1 Judge

Judge 只读取：

- question；
- reference answer；
- prediction；
- 对话时间锚点。

输出：

```text
C
P
I
```

Judge 不读取记忆和搜索轨迹，也不负责根因判断。

### 7.2 Answer Diagnosis

Answer Diagnosis 只读取回答模型实际可见的搜索结果。

如果参考答案的必要事实已经全部进入上下文，但运行模型仍然答错，则记录 Answer Failure。

它不生成修复包，也不进入 Skill 学习。

### 7.3 Access Diagnosis

Access Diagnosis 只读取：

- 当前相关记忆；
- 自然搜索链返回的当前记忆；
- 问题和参考答案；
- Access Skill trace。

它不读取：

- raw conversation；
- 历史记忆版本；
- 构建候选和决策；
- Construction Diagnosis。

模型判断哪些当前条目有用，程序计算有用集合与已检索集合的差。

### 7.4 Construction Diagnosis

Construction Diagnosis 分两阶段：

1. 只根据当前记忆判断必要事实是 FULL、PARTIAL、MISSING 还是 INCORRECT；
2. 仅在发现问题后，读取 raw evidence、候选、决策、commit、change 和 before/after。

它按时间定位第一个错误，只输出最早一个修复目标。

### 7.5 并发和物理隔离

Answer 先完成，Access 与 Construction 后续可以并行。

三个方向拥有独立的：

- progress；
- errors；
- summary；
- manifest；
- 输出目录；
- 模型消息上下文。

Access 与 Construction 可以同时成立，但不生成合并标签。

---

## 8. Skill Learning 实现

### 8.1 Candidate Skill Agent

每个有效诊断包独立产生一个 Candidate，或返回无需修改。

Agent 可以看到：

- 完整诊断包；
- selected Skill；
- nearby-not-selected Skill；
- Official Bank version。

它可以输出：

```text
PROPOSE_SKILL
NO_CHANGE_ALREADY_COVERED
NO_CHANGE_NOT_A_SKILL_PROBLEM
```

Candidate 额外保存 `solves`，用一小段话说明它解决的一般问题。

### 8.2 Candidate 与 Official 隔离

目录和读取权限保证：

- Candidate 不会被 Runtime 检索；
- Access 和 Construction Candidate 分区；
- Candidate 不会通过文件移动直接上线；
- Official Bank 只能通过事务发布。

### 8.3 Candidate 聚类

Candidate 向量为：

```text
0.45 × description embedding
+ 0.35 × content embedding
+ 0.20 × solves embedding
```

使用确定性 spherical K-means，目标组大小为 8，普通 CRUD batch 上限为 10。

聚类后再根据：

- 共享 Official Skill；
- 高词法重合；
- batch 上限；

进行规则修正。

### 8.4 Candidate × Official Skill 检索

每组 Candidate 与该 side 的全部 Official Skill 计算精确相似度矩阵：

```text
0.50 × description semantic
+ 0.30 × content semantic
+ 0.20 × BM25
```

系统保证每个 Candidate 的邻居覆盖，并加入组级公共 Skill，最终上下文上限为 25。

### 8.5 Batch CRUD

CRUD Agent 不读取诊断包和 Runtime trace，只读取：

- Candidate；
- solves；
- 相似度关系；
- 相关 Official Skill。

它可以：

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

程序校验 candidate coverage、目标、side、expected version、content index 和 base Bank version。

### 8.6 冲突和发布

不同 Candidate group 先在同一冻结 Bank 上规划。若两个 plan 修改同一 Skill：

1. 程序计算写集合冲突；
2. 合并相互冲突的 group；
3. 重新检索；
4. 重新调用 CRUD Agent。

冲突消除后，每个 side 一轮最多发布一个 Official Bank version。

---

## 9. 模型与技术组件

| 角色 | 当前实现 |
|---|---|
| Runtime 模型 | Qwen3-8B non-thinking |
| Judge / Diagnosis / Skill | DeepSeek-V4-Pro |
| 模型接口 | OpenAI-compatible、Anthropic、Mock |
| 语言 | Python 3.12 |
| Schema | Pydantic 2 |
| 配置 | PyYAML |
| 数据库 | SQLite、WAL、Foreign Keys、FTS5 |
| Embedding | Sentence-Transformers all-MiniLM-L6-v2 |
| 数值计算 | NumPy |
| 词面评测 | NLTK-based normalization 和 Token F1 |
| 并发 | ThreadPoolExecutor |
| 测试 | Pytest + MockClient |
| 运行产物 | JSON、JSONL、manifest、hash |

所有机器提示词使用英文，用户报告使用中文。模型密钥属于配置层，不是方法的一部分。

---

## 10. 数据与评测

LoCoMo 使用 conversation-level 6:2:2：

```text
Train:
conv-30, conv-42, conv-43, conv-44, conv-48, conv-49

Validation:
conv-26, conv-41

Test:
conv-47, conv-50
```

训练集用于答案、Judge、Diagnosis 和 Skill 学习；验证集用于 Bank 版本和参数选择；测试集
只运行冻结系统。

评测包含：

- Token F1；
- C/P/I LLM-as-Judge；
- category breakdown；
- protocol errors；
- token、steps 和 latency；
- Diagnosis 类型；
- Candidate 和 CRUD 统计；
- Skill selected/nearby/effective 轨迹。

---

## 11. 当前实现状态

### 已实现

- 可追踪版本记忆；
- 自主 ReAct Access & Answer；
- 混合多路检索；
- 三路隔离诊断；
- Construction 首错溯源；
- Runtime Skill trace；
- Candidate/Official 隔离；
- Candidate 聚类；
- Batch CRUD；
- 写冲突重规划；
- Official Bank version；
- Judge-first Skill pipeline 入口；
- 冻结评测接口。

### 尚未由正式结果证明

- Candidate 的整体抽象质量；
- Skill 在 validation/test 上的自然召回；
- MiM 对 C/P/I 和 Token F1 的实际提升；
- Joint 是否优于 Access-only 和 Construction-only；
- 批量 CRUD 是否比逐问题更新更稳定；
- 多轮 Skill Bank 是否持续提升；
- LangMem-Agentic 和 MIRIX 上的跨基座效果。

---

# 第二部分：工程机制如何转换成学术方法

## 12. 总体转换原则

工程对象不能原样成为论文贡献。应采用以下转换：

```text
具体类和文件
  ↓
稳定功能关系
  ↓
方法抽象
  ↓
数学对象
  ↓
可验证研究主张
```

例如：

```text
SQLite 中的 version/message edge
  ↓
记忆条目与原始证据的来源关系
  ↓
可追踪记忆状态
  ↓
provenance graph
  ↓
能否提高 Construction 首错定位准确率
```

论文写的是最后三层，不写第一层。

---

## 13. 工程对象到学术概念的映射

| 工程实现 | 学术概念 | 推荐论文表述 |
|---|---|---|
| Construction Agent | 记忆构建策略 \(\pi_{\mathrm{con}}\) | 从交互和已有状态产生新记忆状态与构建轨迹 |
| Access & Answer ReAct | 闭环自适应访问策略 \(\pi_{\mathrm{acc}}\) | 根据中间证据动态选择查询、检查或停止 |
| SQLite snapshot | 离散记忆状态 \(D_t\) | 某一系统时间点可访问的记忆集合 |
| memory version | 状态转移结果 \(m_i^{(v)}\) | 同一逻辑记忆在不同提交后的内容状态 |
| message edge | 来源关系 \(E_{\mathrm{src}}\) | 记忆声明与原始证据的可追踪映射 |
| parent edge | 版本继承关系 \(E_{\mathrm{par}}\) | 更新和合并的父子依赖 |
| change event | 状态转移算子 \(o_t\) | ADD/UPDATE/MERGE/DELETE 对记忆状态的作用 |
| access actions | 策略轨迹 \(\tau_q^{\mathrm{acc}}\) | 查询、工具观察和停止决策序列 |
| skill trace | 策略暴露轨迹 \(T_q\) | 某次决策实际获得的 Skill 与边界近邻 |
| Answer Diagnosis | 证据充分性检验 | 判断错误是否发生在证据利用阶段 |
| Access set difference | 访问遗漏检验 | 当前有用信息与自然访问结果的集合差 |
| Construction Stage A/B | 渐进式因果定位 | 先确认终态损失，再按需开放历史寻找首错 |
| first error | 最早不变量破坏点 \(t^*\) | 信息忠实性第一次被破坏的状态转移 |
| Candidate Skill | 修复假设 \(c_i\) | 由单个故障归纳出的未发布程序性策略 |
| no-change | 已覆盖或非策略问题 | 防止 Skill Bank 无条件增长 |
| candidates/official | 两阶段策略晋升 | 未发布修复假设与运行策略集合的隔离 |
| K-means groups | 语义故障簇 \(\mathcal{C}_g\) | 将共享修复结构的 Candidate 分组 |
| candidate × bank matrix | 候选—已有策略关系 \(R\) | 为每个 Candidate 显式计算相关策略 |
| CRUD operations | Skill Bank 演化算子 \(\Omega_K\) | 新增、修订、合并、迁移和失效 |
| write-set conflict | 策略更新冲突图 | 两个更新计划共同修改同一 Skill |
| bank_vNNN | 离散策略检查点 \(K^{(v)}\) | 可回滚、可比较的 Skill Bank 状态 |
| selected.json | 冻结部署策略 \(K^*\) | 由验证集选择并用于测试的版本 |
| 6:2:2 split | 会话隔离评测协议 | 防止同一人物和历史跨集合泄漏 |

---

## 14. 核心系统公式

### 14.1 记忆构建

工程上：

```text
session → extract candidates → CRUD decisions → SQLite commit
```

论文中：

\[
D_t,\tau_t^{\mathrm{con}}
=
\pi_{\mathrm{con}}
\left(
x_t,D_{t-1};S_t^{\mathrm{con}}
\right)
\]

其中：

- \(x_t\) 是输入会话；
- \(D_{t-1}\) 是已有记忆状态；
- \(S_t^{\mathrm{con}}\) 是召回的 Construction Skills；
- \(D_t\) 是新状态；
- \(\tau_t^{\mathrm{con}}\) 是构建轨迹。

### 14.2 自适应访问

工程上：

```text
LLM → search → observation → search/inspect/answer
```

论文中：

\[
a_j
\sim
\pi_{\mathrm{acc}}
\left(
a_j\mid q,D_t,S_q^{\mathrm{acc}},h_{<j}
\right)
\]

\[
h_j
=
h_{j-1}\cup\{a_j,o_j\}
\]

其中 \(h_j\) 是累积的 ReAct 历史。若 \(a_j=\textsc{Answer}\)，过程停止并产生
\(\hat y\)。

这种写法比“我们设置最多 6 步、4 次搜索”更学术。具体预算放实验设置。

### 14.3 混合检索

工程上：

```text
semantic + BM25 + keyword + structured → weighted RRF
```

论文中：

\[
s(m\mid q)
=
\sum_{r\in\mathcal{R}}
\frac{w_r}{k+\operatorname{rank}_r(m)}
\]

\[
\tilde{s}(m\mid q)
=
s(m\mid q)
\cdot\gamma_{\mathrm{entity}}
\cdot\gamma_{\mathrm{time}}
\cdot\gamma_{\mathrm{state}}
\]

具体权重放 Implementation Details，不要在方法开头占据叙事中心。

### 14.4 Skill 检索

\[
\mathcal{S}_q
=
\operatorname{TopK}_{s\in K_z}
\left[
\lambda\operatorname{sim}_{\mathrm{sem}}(q,s)
+
(1-\lambda)\operatorname{sim}_{\mathrm{lex}}(q,s)
\right]
\]

其中 \(z\in\{\mathrm{access},\mathrm{construction}\}\)。

工程上的 Top-3 和 disclose-5 可写成：

\[
T_q
=
\left(
\mathcal{S}_q^{\mathrm{selected}},
\mathcal{S}_q^{\mathrm{nearby}},
v_K
\right)
\]

这里 \(T_q\) 表示一次决策的策略暴露轨迹。

---

## 15. 三类错误的学术公式

设 Judge 判断 \(J(\hat y,y^*)\in\{C,P,I\}\)。

### Answer Failure

\[
F_{\mathrm{ans}}
=
\mathbb{I}
\left[
J(\hat y,y^*)\neq C
\land
\operatorname{Cover}(M_{\mathrm{visible}},y^*)=1
\right]
\]

含义：回答错误，同时可见信息已经覆盖必要事实。

### Access Failure

先由语义判断得到当前有用记忆：

\[
M_{\mathrm{useful}}
=
\left\{
m\in D_t:
\operatorname{Support}(m,y^*)=1
\right\}
\]

自然搜索链返回：

\[
M_{\mathrm{retrieved}}
=
\bigcup_j M_j
\]

则：

\[
F_{\mathrm{acc}}
=
\mathbb{I}
\left[
M_{\mathrm{useful}}
\setminus
M_{\mathrm{retrieved}}
\neq\varnothing
\right]
\]

### Construction Failure

\[
F_{\mathrm{con}}
=
\mathbb{I}
\left[
\operatorname{Support}(X_{\mathrm{raw}},y^*)=1
\land
\operatorname{Cover}(D_t,y^*)<1
\right]
\]

首错位置：

\[
t^*
=
\min
\left\{
t:
\mathcal{I}(D_{t-1},X_{\mathrm{raw}})=1
\land
\mathcal{I}(D_t,X_{\mathrm{raw}})=0
\right\}
\]

\(\mathcal{I}\) 表示目标信息仍被忠实保留的不变量。

---

## 16. Candidate 和 Skill Bank 的学术公式

### 16.1 Candidate 归纳

\[
c_i
=
G_{\mathrm{cand}}
\left(
d_i,T_i
\right)
\]

其中：

- \(d_i\) 是诊断包；
- \(T_i\) 是对应 Runtime Skill trace；
- \(c_i\) 是 Candidate Skill。

Agent 也可以输出空操作：

\[
c_i=\varnothing
\]

表示已有策略足够或当前问题不应通过 Skill 修复。

### 16.2 Candidate 表示和聚类

\[
e(c_i)
=
0.45e_{\mathrm{desc}}(c_i)
+
0.35e_{\mathrm{content}}(c_i)
+
0.20e_{\mathrm{solves}}(c_i)
\]

\[
\{\mathcal{C}_1,\ldots,\mathcal{C}_G\}
=
\operatorname{Cluster}
\left(
\{e(c_i)\}_{i=1}^N
\right)
\]

### 16.3 Candidate—Bank 关系

\[
R_{ij}
=
0.50\operatorname{sim}
\left(
d_i^{\mathrm{desc}},s_j^{\mathrm{desc}}
\right)
+
0.30\operatorname{sim}
\left(
d_i^{\mathrm{content}},s_j^{\mathrm{content}}
\right)
+
0.20\operatorname{BM25}(c_i,s_j)
\]

论文中应把“统一召回”表述为：

> 对每个语义 Candidate group，显式建模每个修复假设与已有策略之间的关系，而不是用一个
> 拼接查询代表整组需求。

### 16.4 Skill Bank 演化

设可用操作集：

\[
\Omega_K
=
\{
\textsc{Add},
\textsc{Rename},
\textsc{UpdateDescription},
\textsc{AddContent},
\textsc{UpdateContent},
\textsc{DeleteContent},
\textsc{MoveContent},
\textsc{DeleteSkill}
\}
\]

CRUD Agent 产生计划：

\[
P_g
=
G_{\mathrm{crud}}
\left(
\mathcal{C}_g,
K_z^{(v)},
R_g
\right)
\]

确定性验证器检查：

\[
\operatorname{Valid}(P_g,K_z^{(v)})=1
\]

才允许状态转移：

\[
K_z^{(v+1)}
=
\operatorname{Apply}
\left(
K_z^{(v)},P_g
\right)
\]

---

## 17. 冲突处理如何学术化

工程上使用并查集检测多个 plan 是否修改同一 Skill。

论文中可定义冲突图：

\[
G_{\mathrm{conflict}}
=
(V,E)
\]

其中每个节点代表一个 CRUD plan，若：

\[
W_i\cap W_j\neq\varnothing
\]

则 \((i,j)\in E\)。

对冲突图的每个连通分量重新生成联合计划。该过程可以表述为：

> 在共享策略库上执行乐观并行规划，并在提交前对重叠写集合进行组件级重规划。

论文不需要说明使用了 union-find；union-find 是实现该抽象的算法细节。

---

## 18. Bank 选择和最终目标

工程上按 validation C rate、I rate、F1 和 Bank 规模选择版本。

论文中可写成词典序目标：

\[
v^*
=
\arg\max_v
\left(
\operatorname{C\text{-}Rate}(K^{(v)}),
-\operatorname{I\text{-}Rate}(K^{(v)}),
\operatorname{F1}(K^{(v)}),
-|K^{(v)}|
\right)
\]

更一般的研究目标为：

\[
\max_K
\quad
Q(K)
-\alpha|K|
-\beta\operatorname{Redundancy}(K)
-\eta\operatorname{Churn}(K)
-\mu\operatorname{Cost}(K)
\]

其中 \(Q(K)\) 是冻结 Skill Bank 下的任务质量。

当前 MVP 并没有直接求解这一连续优化问题，而是通过离散 Candidate、CRUD 和版本选择近似
实现。论文应明确这是离散策略库演化，而不是梯度训练。

---

## 19. 推荐的语言转换示例

### 示例一：物理目录隔离

不推荐：

> Candidate 放在 `skills/candidates`，正式 Skill 放在 `skills/official`。

推荐：

> MiM 采用两阶段策略晋升机制：由单个故障归纳的修复假设首先进入不可执行的候选集合，
> 只有在全局整合和一致性验证后才被发布到运行策略库。

### 示例二：Access 不看历史

不推荐：

> Access Agent 不能读取旧版本表。

推荐：

> Access attribution 在固定的当前记忆快照上进行，以隔离访问策略错误与上游构建历史；
> 历史版本仅在 Construction attribution 已确认终态信息损失后按需开放。

### 示例三：Access 与 Construction 独立

不推荐：

> 两个脚本并行跑，输出两个文件夹。

推荐：

> MiM 将访问遗漏与构建损失建模为两个非互斥的因果检验，它们使用不相交的信息集并产生
> 独立修复目标。

### 示例四：Skill trace

不推荐：

> 保存 Top-3 和后五个 Skill。

推荐：

> 系统记录每次策略决策的实际 Skill 暴露集合及检索边界附近的候选，使后续学习能够区分
> 表示缺失、召回失败和执行失败。

### 示例五：批量 CRUD

不推荐：

> 每十个 Candidate 调一次 CRUD，减少 API 次数。

推荐：

> 系统先从实例级故障独立归纳修复假设，再在语义相近的假设簇上执行策略库整合，从而降低
> 在线顺序依赖、重复策略和版本更新振荡。

### 示例六：SQLite 来源边

不推荐：

> 数据库中有 message edge 和 parent edge。

推荐：

> 每个记忆状态保留原始证据来源和版本继承关系，形成可遍历的 provenance graph，用于定位
> 信息忠实性首次被破坏的状态转移。

---

## 20. 哪些内容放论文正文，哪些放实现细节

### 正文保留

- 大模型介导记忆系统定义；
- 构建与访问策略分解；
- 三类错误的形式化；
- provenance 与首错定位；
- Candidate 与 Official 的两阶段晋升；
- Candidate 聚类和 Candidate—Bank 关系；
- CRUD 与冲突重规划；
- 冻结验证协议；
- 主要研究假设。

### Implementation Details

- Qwen3-8B 和 DeepSeek-V4-Pro；
- SQLite、MiniLM、BM25 和 RRF；
- 各项具体权重；
- top-k、disclose-k；
- 最大 ReAct steps；
- batch size；
- concurrency workers；
- temperature 和 token budget。

### Appendix 或开源文档

- 数据库表；
- JSON Schema；
- 目录树；
- CLI；
- resume 规则；
- prompt 全文；
- manifest 字段；
- API provider 配置。

---

## 21. 最重要的写作边界

### 不要把可维护性本身写成核心创新

目录隔离、Pydantic、SQLite 事务和 JSONL 恢复非常重要，但它们是支持方法可信度的工程基础，
不是论文贡献本身。

### 不要把参数写成方法

Top-3、0.85/0.15、batch size 10 是当前实现选择。方法贡献是“记录策略暴露轨迹”
“按语义候选簇整合”和“Candidate—Bank 显式关系”，不是这些具体数字。

### 不要把 LLM 判断写成确定真值

Diagnosis 是由强模型执行的语义归因，需要通过人工抽查、可控故障集和消融验证。

### 不要把未完成实验写成能力

跨基座、跨数据集、多 Epoch、自然召回复用仍是待验证主张。

### 不要把 Single-Agent 基座包装成新的强记忆架构

它的价值是可控、透明和可诊断，用于验证 MiM 的机制。

---

## 22. 论文方法的一句话抽象

> MiM 将回答失败分解为证据利用、记忆访问和记忆构建问题，并将可归因的访问与构建故障
> 转化为候选程序性策略；这些策略经全局关系建模、冲突约束和版本化发布后，在后续运行中
> 作为可检索的元记忆指导原有记忆系统。


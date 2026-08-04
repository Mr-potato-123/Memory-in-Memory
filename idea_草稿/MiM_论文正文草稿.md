# Memory in Memory: Learning Procedural Memory Policies from Downstream Failures

> **中文论文正文草稿**  
> **范围：Abstract、Introduction、Problem Formulation 与 Method**

---

# 摘要

大语言模型智能体通常借助外部记忆跨会话保存用户事实、状态和历史事件。然而，现有研究
主要关注记忆内容的表示、压缩和检索，较少研究记忆系统如何从自身错误中持续学习“以后应当
如何记忆”。当下游任务失败时，相同的错误答案可能来自完全不同的阶段：目标信息可能从未
被正确写入，可能已经存在却没有被访问，也可能已经进入上下文但没有被回答模型正确利用。
如果不区分这些根因，直接从错误生成反思或修改提示词，容易产生错误归因、重复规则和全局
回归。

本文提出 Memory in Memory（MiM），一种面向大模型介导记忆系统的错误驱动程序性
元记忆框架。MiM 将长期记忆系统分解为记忆构建策略和记忆访问策略，并在正常运行之外建立
一个离线错误学习过程。给定下游失败，MiM 使用相互隔离的信息视图分别检验回答证据是否
充分、当前有用记忆是否被自然访问，以及原始信息是否在构建过程中被遗漏或破坏。对于访问
故障，系统比较当前有用记忆和自然搜索轨迹；对于构建故障，系统利用来源和版本关系定位
信息忠实性第一次被破坏的状态转移。

MiM 将确认的故障归纳为自然语言 Candidate Skill。Candidate 并不立即进入运行系统，
而是按访问和构建方向分别聚类，与已有 Skill Bank 建立显式关系，并通过受约束的批量
增删改并产生新的策略库版本。运行时只检索冻结的正式 Skill，同时记录实际使用的策略和
检索边界附近的候选，使后续学习能够区分策略缺失、召回失败和执行无效。由此，长期记忆
系统积累的不再只有关于用户和世界的对象级事实，也包括关于自身应如何构建和访问记忆的
程序性经验。

---

# 1. Introduction

外部记忆已经成为长期运行大语言模型智能体的基础组件。一个具有持久记忆的智能体可以跨
会话保留用户偏好、人物关系、历史事件和状态变化，并在未来任务中选择性地恢复相关信息。
围绕这一目标，现有工作提出了向量记忆、分层摘要、结构化事实、时间图、多类型记忆和
Agent-managed memory 等多种方案。尽管底层表示不断增强，长期记忆系统仍然反复出现相似
错误：重要信息没有被保留，更新事实破坏了历史状态，语义相似但含义不同的事件被错误合并，
目标事实已经存在却没有被检索，或多跳问题只获得部分证据便提前回答。

这些错误并不完全来自存储容量或检索器性能。对于由大模型介导的记忆系统，关键决策发生在
策略层：模型决定哪些信息值得写入，如何把新信息与已有记忆整合，当前问题需要检索什么，
是否应当继续搜索，以及何时已有证据足以支持回答。即使底层数据库能够精确保存和返回数据，
不恰当的构建与访问策略仍然会产生系统性失败。

现有系统通常积累对象级记忆，即关于用户、环境和历史的事实。相比之下，它们很少显式积累
程序性元记忆，即关于记忆系统自身操作方式的经验。例如，一个系统可以保存“用户已经搬到
上海”，却未必会从先前错误中学习“更新当前地址时应保留旧地址的历史有效区间”；它也可以
保存多个相关事件，却未必会学习“列表问题在只找到一个组成部分时不应停止检索”。

一种直接方案是保存失败案例，并在遇到相似问题时检索历史错误；另一种方案是将所有反思
不断追加到全局提示词。这两种方式都缺少关键的根因约束。错误答案只是一种下游表象，它
可能来自信息没有进入记忆、信息没有被访问，或信息已经可见但未被正确利用。若系统把访问
遗漏归纳为构建规则，或者把回答推理错误写成检索策略，新增经验不仅无法复用，还可能损害
其他任务。

因此，从错误中学习记忆策略首先需要回答一个因果问题：

> 在原始交互、记忆状态、访问轨迹和回答上下文构成的流水线中，目标信息第一次在哪里没有
> 被正确保留或使用？

本文提出 Memory in Memory（MiM），将下游错误转化为可归因、可维护和可复用的
程序性记忆策略。MiM 不替换底层记忆系统，而是在其外部增加一个控制平面。正常运行阶段，
基座仍按照原有方式构建和访问记忆；离线学习阶段，MiM 观察原始交互、记忆版本、自然搜索
轨迹和回答结果，分别诊断证据利用、记忆访问和记忆构建故障。

MiM 的诊断不是单一模型输出的互斥标签。回答充分性、访问遗漏和构建损失被建模为三个使用
不同信息集的检验。访问诊断只在固定的当前记忆快照上比较有用信息和自然搜索结果，不读取
原始对话或历史版本；构建诊断先检查当前记忆是否完整，仅在发现问题时沿来源关系和版本链
定位最早错误。一个问题可以同时包含访问和构建问题，因为当前记忆可能丢失一个事实，同时
又没有召回另一个仍然存在的事实。

对于确认的访问或构建故障，MiM 不直接把完整失败案例写入运行上下文，而是生成简短、
可检索的自然语言 Candidate Skill。Candidate 描述某类情形何时出现，以及记忆 Agent
应采取什么操作原则。为避免逐案例更新造成策略库膨胀，MiM 先独立归纳 Candidate，再在
语义相近的 Candidate 集合上检索已有 Skill，并执行受约束的批量增删改并。Candidate
只有经过全局整合和一致性验证，才会成为正式运行策略。

MiM 还记录每次运行实际获得的 Skill 及检索边界附近的候选。这一策略暴露轨迹使系统能够
在下一轮区分三种不同失败：策略库中不存在相关经验，相关经验存在但没有进入上下文，以及
相关经验已经被使用但内容仍然无效。由此，Skill 学习不再是不可观察的提示词修改，而形成
一个可以分析表示、召回和执行三个阶段的闭环。

本文的核心贡献如下：

1. 我们提出错误驱动程序性元记忆这一问题设置，使长期记忆系统能够积累关于自身构建和访问
   策略的可复用经验，而不仅积累对象级事实。
2. 我们提出一种信息隔离的记忆故障归因方法，将回答充分性、访问遗漏和构建损失分解为非
   互斥检验，并利用来源与版本关系定位构建过程中的最早错误。
3. 我们提出访问与构建分治的双 Skill Bank，以及从实例级 Candidate 到正式策略的两阶段
   晋升机制。
4. 我们提出基于 Candidate—Bank 关系、批量 CRUD 和冲突重规划的策略库演化方法，以控制
   重复、顺序依赖和版本振荡。
5. 我们构建一个完全可观察的长期记忆研究基座，并设计 conversation-level 冻结评测，
   用于分别测量最终任务质量、错误类型变化和 Skill 的自然调用。

---

# 2. Problem Formulation

## 2.1 LLM-mediated Memory Systems

我们考虑一个由大模型介导的长期记忆系统：

\[
\mathcal{M}
=
\left(
\mathcal{D},
\pi_{\mathrm{con}},
\pi_{\mathrm{acc}},
f_{\mathrm{ans}}
\right),
\]

其中 \(\mathcal{D}\) 表示持久记忆状态，\(\pi_{\mathrm{con}}\) 表示记忆构建策略，
\(\pi_{\mathrm{acc}}\) 表示记忆访问策略，\(f_{\mathrm{ans}}\) 表示根据问题和证据产生
答案的任务模型。

给定第 \(t\) 个交互片段 \(x_t\) 和先前记忆状态 \(D_{t-1}\)，构建策略产生：

\[
D_t,\tau_t^{\mathrm{con}}
=
\pi_{\mathrm{con}}
\left(
x_t,D_{t-1};S_t^{\mathrm{con}}
\right),
\]

其中 \(S_t^{\mathrm{con}}\) 是本轮可用的 Construction Skills，
\(\tau_t^{\mathrm{con}}\) 是构建轨迹。

给定问题 \(q\)，访问策略通过一系列动作 \(a_1,\ldots,a_J\) 与记忆交互：

\[
a_j
\sim
\pi_{\mathrm{acc}}
\left(
a_j\mid q,D_t,S_q^{\mathrm{acc}},h_{<j}
\right),
\]

其中 \(h_{<j}\) 包含此前的查询、工具结果和中间判断。当策略执行停止动作时，回答模型根据
累积可见证据 \(M_{\mathrm{visible}}\) 产生：

\[
\hat y=f_{\mathrm{ans}}(q,M_{\mathrm{visible}}).
\]

在当前实现中，访问和回答由同一闭环 Agent 完成，因此停止决策和最终回答共享完整的自然
搜索上下文。

---

## 2.2 Object-level and Procedural Memory

对象级记忆 \(D_t\) 保存用户和世界事实。MiM 额外维护两类程序性策略库：

\[
K
=
\left(
K_{\mathrm{con}},
K_{\mathrm{acc}}
\right).
\]

\(K_{\mathrm{con}}\) 指导信息选择、抽取、更新、合并和历史保持；
\(K_{\mathrm{acc}}\) 指导查询规划、检索路线、过滤、证据聚合和停止。

每个 Skill 采用最小自然语言表示：

\[
s=(n,d,c),
\]

其中 \(n\) 是名称，\(d\) 是检索描述，\(c\) 是执行内容。Skill 不保存训练案例中的用户事实，
而描述可以迁移到其他实体和会话的处理原则。

---

## 2.3 Failure Signal

给定参考答案 \(y^*\) 和模型输出 \(\hat y\)，语义 Judge 输出：

\[
J(\hat y,y^*)\in\{C,P,I\}.
\]

只有 \(P\) 和 \(I\) 进入错误归因。Judge 本身不读取记忆轨迹，因此只确认下游失败，不解释
失败原因。

设 \(\operatorname{Cover}(M,y^*)\) 表示证据集合 \(M\) 对参考答案必要事实的覆盖程度，
\(\operatorname{Support}(m,y^*)\) 表示记忆 \(m\) 是否支持至少一个必要事实。

MiM 定义三种诊断结果。

**Answer Failure**

\[
F_{\mathrm{ans}}
=
\mathbb{I}
\left[
J(\hat y,y^*)\neq C
\land
\operatorname{Cover}(M_{\mathrm{visible}},y^*)=1
\right].
\]

**Access Failure**

\[
M_{\mathrm{useful}}
=
\left\{
m\in D_t:
\operatorname{Support}(m,y^*)=1
\right\},
\]

\[
M_{\mathrm{retrieved}}
=
\bigcup_{j=1}^{J}M_j,
\]

\[
F_{\mathrm{acc}}
=
\mathbb{I}
\left[
M_{\mathrm{useful}}
\setminus M_{\mathrm{retrieved}}
\neq\varnothing
\right].
\]

**Construction Failure**

\[
F_{\mathrm{con}}
=
\mathbb{I}
\left[
\operatorname{Support}(X_{\mathrm{raw}},y^*)=1
\land
\operatorname{Cover}(D_t,y^*)<1
\right].
\]

这些结果不是互斥分类。尤其是 \(F_{\mathrm{acc}}\) 和 \(F_{\mathrm{con}}\) 可以同时成立。

---

# 3. Memory in Memory

## 3.1 Overview

MiM 在基座记忆系统之外增加错误学习控制平面。一次完整迭代包含三个阶段：

1. 基座在冻结 Skill Bank 下构建记忆并回答训练问题；
2. MiM 对语义错误执行信息隔离的根因诊断，并形成 Candidate Skills；
3. Candidate 被全局整理为新的 Skill Bank 版本，并在后续运行中自然调用。

该设计将实例级修复和运行策略发布分开。诊断回答“当前失败需要学习什么”，全局整理回答
“这一修复与已有策略之间是什么关系”，冻结评测回答“更新后的策略库是否在未见会话上有效”。

---

## 3.2 Traceable Memory States

为了定位构建错误，MiM 要求记忆系统提供来源和版本关系。我们将每个记忆状态表示为：

\[
D_t=(V_t,E_{\mathrm{src}},E_{\mathrm{par}},E_{\mathrm{chg}}),
\]

其中 \(V_t\) 是在系统时间 \(t\) 可见的记忆版本，\(E_{\mathrm{src}}\) 将版本连接到原始消息，
\(E_{\mathrm{par}}\) 表示更新或合并的父版本关系，\(E_{\mathrm{chg}}\) 表示产生新版本的状态
转移。

每个状态转移由操作：

\[
o_t\in
\{
\textsc{Add},
\textsc{Update},
\textsc{Merge},
\textsc{Delete},
\textsc{Skip}
\}
\]

产生。来源关系随更新继承，因此系统可以从任一原始消息追踪其形成的候选、初始版本和后续
变化。

这种表示允许将构建诊断从“最终记忆是否错误”提升为“信息在哪次状态转移中第一次被破坏”。
设 \(\mathcal{I}(D_t,X)\) 表示记忆状态 \(D_t\) 仍忠实保留来源证据 \(X\) 中的目标信息，
则最早错误点为：

\[
t^*
=
\min
\left\{
t:
\mathcal{I}(D_{t-1},X)=1
\land
\mathcal{I}(D_t,X)=0
\right\}.
\]

如果初始抽取已经错误，则 \(t^*\) 对应候选形成；如果初始状态正确，\(t^*\) 对应第一次破坏
该信息的更新、合并或删除。

---

## 3.3 Closed-loop Memory Access

MiM 不把访问建模为一次固定检索。访问策略在每轮观察后选择搜索、检查或回答动作。对第
\(j\) 轮，策略接收此前历史 \(h_{<j}\)，生成动作 \(a_j\)，工具返回观察 \(o_j\)，并更新：

\[
h_j=h_{j-1}\cup\{a_j,o_j\}.
\]

搜索动作可以在语义、词法、精确匹配和结构化时间路线之间选择。对于混合检索，候选分数为：

\[
s(m\mid q)
=
\sum_{r\in\mathcal{R}}
\frac{w_r}{k+\operatorname{rank}_r(m)},
\]

并根据实体匹配、目标时间有效性和当前状态进行校正。

Agent 根据累积证据判断必要事实是否完整。若信息不足，它可以分解问题、改变查询表达、
切换路线或扩大深度；若信息充分，则立即停止。这一闭环为 Access Skill 提供了真实可控制
对象：Skill 可以影响的不只是最终查询文本，还包括检索路线、继续条件和证据完整性判断。

---

## 3.4 Isolated Failure Attribution

MiM 对同一个错误建立三个独立信息视图。

回答视图只包含运行模型实际看见的证据，用于判断答案错误是否发生在证据利用阶段。

访问视图固定在当前记忆快照，只包含当前相关记忆和自然访问轨迹。它不读取原始对话和历史
版本，以免将上游构建错误引入访问诊断。语义模型标记有用条目，确定性程序计算：

\[
M_{\mathrm{missing}}
=
M_{\mathrm{useful}}
\setminus
M_{\mathrm{retrieved}}.
\]

构建视图采用渐进式开放。第一阶段只检查当前相关记忆对必要事实的覆盖；只有发现
PARTIAL、MISSING 或 INCORRECT 时，第二阶段才读取来源消息和版本轨迹，并定位 \(t^*\)。

这种权限设计的目的不是减少模型输入，而是建立可解释的因果边界。Access Failure 意味着
“当前已有信息没有被访问”，Construction Failure 意味着“当前信息状态本身已经错误”。

---

## 3.5 Runtime Skill Exposure

运行时从方向对应的 Skill Bank 检索：

\[
\mathcal{S}_q
=
\operatorname{TopK}_{s\in K_z}
\left[
\lambda\operatorname{sim}_{\mathrm{sem}}(q,s)
+
(1-\lambda)\operatorname{sim}_{\mathrm{lex}}(q,s)
\right],
\]

其中 \(z\in\{\mathrm{acc},\mathrm{con}\}\)。

除了实际注入的 Skill，系统还记录检索边界附近但没有进入上下文的候选：

\[
T_q
=
\left(
\mathcal{S}_q^{\mathrm{selected}},
\mathcal{S}_q^{\mathrm{nearby}},
v_K
\right).
\]

\(T_q\) 是后续学习的策略暴露轨迹。若一个相关 Skill 已经进入
\(\mathcal{S}_q^{\mathrm{selected}}\)，错误更可能来自 Skill 内容或执行；若它只出现在
\(\mathcal{S}_q^{\mathrm{nearby}}\)，问题更可能来自检索表示；若两者都不存在，系统可能需要
学习新的策略。

---

## 3.6 Failure-conditioned Skill Induction

对于诊断包 \(d_i\) 和对应的策略暴露轨迹 \(T_i\)，Candidate Agent 产生：

\[
c_i
=
G_{\mathrm{cand}}(d_i,T_i).
\]

Candidate 包含名称、检索描述、执行内容和一个简短的故障摘要。生成过程需要移除具体实体、
日期、答案和内部 ID，以保留跨案例复用性。

Candidate Agent 也可以输出：

\[
c_i=\varnothing,
\]

表示现有策略已经覆盖该问题，或该问题不适合通过记忆 Skill 修复。空操作是控制 Skill Bank
增长的重要机制，而不是生成失败。

Candidate 在发布前不能被运行策略检索。MiM 因此形成两阶段策略生命周期：实例级故障先
产生不可执行修复假设，再由全局整理过程决定其新增、合并、更新或拒绝。

---

## 3.7 Semantic Candidate Consolidation

逐错误更新策略库会产生强顺序依赖。前一个错误发布的 Skill 会改变后一个错误看到的 Bank，
相似故障可能连续创建重复规则，同一 Skill 也可能被频繁改写。MiM 因此先收集 Candidate，
再进行批量整合。

Candidate 的语义表示为：

\[
e(c_i)
=
\alpha e_{\mathrm{desc}}(c_i)
+
\beta e_{\mathrm{content}}(c_i)
+
\gamma e_{\mathrm{solves}}(c_i),
\]

其中 \(\alpha+\beta+\gamma=1\)。语义相近 Candidate 被划分为候选簇
\(\{\mathcal{C}_g\}_{g=1}^{G}\)。

对于 Candidate \(c_i\) 和已有 Skill \(s_j\)，MiM 显式计算关系：

\[
R_{ij}
=
\lambda_d\operatorname{sim}
\left(
c_i^{d},s_j^{d}
\right)
+
\lambda_c\operatorname{sim}
\left(
c_i^{c},s_j^{c}
\right)
+
\lambda_l\operatorname{lex}(c_i,s_j).
\]

与把整组 Candidate 拼成一条查询相比，矩阵 \(R\) 保留每个修复假设自己的相关 Skill，
同时允许发现覆盖多个 Candidate 的公共策略。

---

## 3.8 Constrained Skill Bank Evolution

对候选簇 \(\mathcal{C}_g\)、相关已有 Skill 和关系矩阵 \(R_g\)，CRUD Agent 产生更新计划：

\[
P_g
=
G_{\mathrm{crud}}
\left(
\mathcal{C}_g,K_z^{(v)},R_g
\right).
\]

计划可以新增、重命名、修改描述、增加或修改内容、移动规则或失效 Skill。每个 Candidate
必须获得唯一处理结果，例如创建、合并、已覆盖或拒绝。

模型只提出计划，确定性验证器检查目标存在性、方向、基础版本、内容位置和 Candidate 覆盖：

\[
\operatorname{Valid}(P_g,K_z^{(v)})=1.
\]

只有合法计划才能修改策略库。

不同候选簇可以在同一冻结版本上并行规划。设 \(W_g\) 是计划 \(P_g\) 的写集合，若：

\[
W_i\cap W_j\neq\varnothing,
\]

两个计划存在冲突。MiM 构造冲突图，并对每个连通分量重新生成联合计划。所有冲突消除后，
更新以单个事务产生新的策略库版本：

\[
K_z^{(v+1)}
=
\operatorname{Apply}
\left(
K_z^{(v)},\bigcup_gP_g
\right).
\]

这一过程将语言模型的语义整合能力与确定性状态管理结合：模型决定策略内容如何组织，程序
保证更新满足一致性和可追踪性。

---

## 3.9 Validation and Frozen Evaluation

MiM 在训练故障上生成 Candidate，在验证会话上选择 Skill Bank 版本，并在测试前冻结：

\[
K^*
=
\arg\max_{K^{(v)}}
\left(
\operatorname{C\text{-}Rate},
-\operatorname{I\text{-}Rate},
\operatorname{F1},
-|K^{(v)}|
\right).
\]

测试阶段不提供参考答案，不执行 Diagnosis，也不更新 Skill Bank。基座和 MiM 使用相同
模型、存储、工具、步骤和 token 预算，唯一差异是冻结 Skill 的检索和注入。

MiM 的长期目标不仅是最大化任务质量，还需要控制策略数量、冗余和更新振荡：

\[
\max_K
\quad
Q(K)
-\alpha|K|
-\beta\operatorname{Redundancy}(K)
-\eta\operatorname{Churn}(K)
-\mu\operatorname{Cost}(K).
\]

当前方法通过离散 Candidate、批量 CRUD 和版本选择近似这一目标，不依赖梯度训练。

---

# 4. Discussion of Design Choices

## 4.1 Why Separate Construction and Access Skills?

构建和访问作用于不同状态。Construction Skill 改变未来记忆状态，错误更新可能影响后续
大量问题；Access Skill 不修改记忆，只改变当前问题如何获得证据。二者的可观察轨迹、失败
条件和验证方式均不同。

单一 Skill Bank 容易使类似词汇但不同因果位置的规则互相干扰。例如，“处理多个时间版本”
既可能表示构建时保留有效区间，也可能表示访问时选择目标时间。MiM 使用方向分离的 Bank
保持修复责任清晰。

---

## 4.2 Why Merge Access and Answer?

访问是否应继续取决于模型对当前证据的理解。若 Access Agent 先生成一个固定证据集合，再把
结果交给独立 Answer Agent，前者无法根据回答需要动态补全缺失信息。

MiM 将检索和回答置于同一 ReAct 上下文，使模型能够在每次观察后判断 FULL、PARTIAL 或
NONE，并自主停止。这一合并不影响错误归因：离线 Answer Diagnosis 仍可以检查运行模型
最终获得的信息是否已经充分。

---

## 4.3 Why Diagnose with Gold but Freeze at Test Time?

参考答案和 evidence 在 MiM 中只承担离线监督作用，用于判断失败以及定位训练阶段的修复
目标。最终测试不提供任何 gold，也不允许根据错误更新 Skill。

这一设置与使用训练标签优化模型参数类似：gold 用于学习策略，但冻结后的系统必须在未见
会话中自然检索并执行这些策略。为了检查潜在的答案泄漏，Skill 需要进行实体、日期、内部
ID 和罕见字符串审计，并在 conversation-disjoint split 上评测。

---

## 4.4 Why Batch Candidate Skills?

Candidate 是从单个故障归纳出的局部修复假设，不一定对应一个独立的长期 Skill。多个错误
可能共享同一原因，一个错误也可能已经被现有 Skill 覆盖。

批量整合使系统先保留实例级归纳的独立性，再进行全局压缩。其研究作用不仅是减少模型调用，
更重要的是降低样本顺序对 Skill Bank 结构的影响，并为重复率、合并率和版本振荡提供显式
测量对象。

---

# 5. Experimental Questions

当前方法应由以下问题验证：

**RQ1：端到端有效性。**  
冻结 MiM Skill 后，长期记忆问答的语义正确率和 Token F1 是否提高？

**RQ2：错误归因。**  
隔离的信息视图能否正确区分 Answer、Access 和 Construction Failure，并定位构建首错？

**RQ3：双侧贡献。**  
MiM-Construction 和 MiM-Access 是否分别减少对应错误，Joint 是否优于单侧版本？

**RQ4：Skill 抽象与复用。**  
由一个训练故障生成的 Skill 能否在未见人物和未见 conversation 中自然召回并修复相似问题？

**RQ5：批量演化。**  
批量 Candidate 整合是否比逐错误更新产生更小、更少重复且更稳定的 Skill Bank？

**RQ6：跨基座适用性。**  
在保持底层工具、模型和预算不变时，MiM 能否增强外部 Agentic Memory Systems？

**RQ7：成本。**  
离线 Diagnosis 和 Skill 学习的额外成本，能否被错误减少和 Skill 复用抵消？

---

# 6. Scope and Limitations

MiM 面向构建和访问策略至少部分由大模型控制、且能够观察或重建运行轨迹的记忆系统。它
不直接适用于完全不可观测的参数记忆，也不能可靠诊断没有来源关系、无法读取当前记忆状态的
封闭服务。

MiM 的诊断依赖强模型进行语义支持判断，因此并不天然等同于真实因果根因。可控故障集、
人工抽查和权限消融仍然必要。LLM-as-Judge 也可能存在偏差，应同时报告词面指标和 Judge
一致性。

此外，Candidate 的自然语言抽象可能过宽或包含训练案例痕迹；批量 CRUD 也可能错误合并
表面相似但边界不同的策略。版本化和 Candidate 隔离降低了风险，但不能替代未见样本上的
冻结验证。

最后，当前 Unified Single-Agent 实现主要用于机制验证。MiM 的可插拔性需要在外部
Agentic Memory Systems 上保持原生 schema、工具集合和预算进行验证，不能仅凭统一接口
设计直接宣称。

---

# 7. Conclusion

本文研究长期记忆系统如何从自身错误中学习可复用的记忆策略。MiM 将下游失败分解为回答
证据利用、当前记忆访问和历史记忆构建问题，并利用来源、版本和自然搜索轨迹形成可审计的
修复目标。确认的访问和构建故障被归纳为方向独立的 Candidate Skills，再通过全局关系建模、
冲突约束和版本化发布形成正式 Skill Bank。

这一框架使记忆系统的长期积累对象从“关于用户和世界的事实”扩展到“关于自身如何记忆的
程序性经验”。更重要的是，它把策略是否存在、是否被召回和是否有效转化为可以分别观察和
评估的过程，为错误驱动的智能体记忆学习提供了一个明确的方法边界。

# Memory in Memory：技术路径与流程说明

## 1. 我们到底在做什么

这个工作的核心不是单纯给记忆系统增加一组固定 Skill，也不是在记忆库里再嵌套一层普通记忆。

我们要做的是：

> 让记忆系统能够积累“自己过去是怎么写错、检索错，以及之后应该怎么改”的经验。

系统中有两层内容：

### 第一层：普通记忆

保存用户历史中的事实，例如：

- 用户偏好
- 历史事件
- 时间变化
- 计划
- 关系
- 助手过去给出的建议

这些内容由底层记忆系统保存，例如 SimpleMem、Mem0 等记忆框架。

### 第二层：关于记忆操作的经验

保存的是：

- 什么情况下容易漏写记忆
- 什么情况下更新操作会删除重要事实
- 什么问题需要时间条件检索
- 什么问题需要多路检索
- 什么 Skill 没被正确触发
- 什么 Skill 被调用后仍然无效

这些经验最后会被整理成可执行的 Skill。

因此，我们可以这样理解：

```text
普通记忆：过去发生了什么
元记忆：以后应该怎样写和怎样查
```

---

## 2. 为什么仍然叫 Memory in Memory

虽然系统内部保存的是 Skill，但这些 Skill 并不是人工直接写好的规则。

它们来自真实历史中的失败过程：

```text
一次问答失败
→ 判断问题来自写入还是检索
→ 分析失败原因
→ 尝试修复
→ 验证修复是否有效
→ 将成功经验总结成通用 Skill
```

因此，Skill 是从具体失败经验中抽象出来的。

具体失败经验是“具象记忆”，而 Skill 是“抽象后的程序性记忆”。

可以概括为：

```text
真实失败经历
→ 诊断
→ 抽象
→ Skill
→ 长期保存
→ 在未来相似问题中调用
```

所以：

- **Memory in Memory** 是系统整体结构；
- **Meta-Memory** 是这层内容的本质；
- **Skill** 是这层记忆的实际表示形式。

---

## 3. 系统中的两个智能体

系统包含两个核心智能体。

## 3.1 Memory Writer Agent

负责构建和更新普通记忆。

主要任务包括：

- 从原始对话中抽取事实
- 压缩对话内容
- 合并重复信息
- 处理新旧状态
- 保留时间、否定、人物和关系信息
- 决定新增、更新、合并或删除记忆

Writer Agent 会调用 Write Skill。

### Write Skill 示例

```text
当同一属性在不同时间出现不同取值时：
1. 保留旧值和新值；
2. 保存时间信息；
3. 标记 historical/current 状态；
4. 不要把两个状态粗暴合并成一条无时间信息的记忆。
```

---

## 3.2 Memory Reader Agent

负责根据问题查找和组织记忆。

主要任务包括：

- 判断问题意图
- 选择相关 Skill
- 改写查询
- 生成关键词
- 增加实体和时间条件
- 决定检索深度
- 合并多路检索结果
- 对候选记忆重新排序

Reader Agent 会调用 Read Skill。

### Read Skill 示例

```text
当问题询问“当前、最新、现在”的状态时：
1. 同时检索当前状态和历史状态；
2. 检索时间戳；
3. 按时间排序；
4. 优先使用最新且未被后续更新覆盖的信息。
```

---

## 4. 整体系统结构

```text
历史对话
   ↓
Memory Writer Agent
   ↓
普通记忆库 MemDB
   ↓
Memory Reader Agent
   ↓
回答问题

问答失败
   ↓
失败诊断
   ↓
生成或修改 Read / Write Skill
   ↓
保存到 Meta-Memory Skill Bank
   ↓
后续 Writer / Reader Agent 按需调用
```

Meta-Memory Skill Bank 分为两部分：

```text
Meta-Memory Skill Bank
├── Read Skill Bank
└── Write Skill Bank
```

---

## 5. 错误类型

当前工作主要处理三类错误。

## Type 1：记忆存在，但没有检索完整

判断条件：

```text
完整 MemDB 中存在答案需要的事实
但最终 retrieved memories 中没有这些事实，或者只检索到一部分
```

处理方式：

```text
优化 Read Skill
```

常见原因：

- Skill 没有被调用, 对应 Skill 摘要不容易被检索到或 Skill 关键词不足
- Query 改写不正确
- 缺少时间或实体条件
- 检索深度不足
- 多条证据没有被同时召回

---

## Type 2：答案所需事实没有被最终正确保留

判断条件：

```text
raw conversation 中存在答案需要的事实
但最终 MemDB 中没有，或者内容被写错
```

Type 2 不只包括“第一次没写进去”，也可能发生在后续更新阶段。

可以继续细分为：

### 2.1 初始遗漏

事实从未被抽取进入记忆库。

### 2.2 初始写错

事实被写入，但：

- 时间丢失
- 否定丢失
- 人物混淆
- 数值写错
- 关系写错
- 当前和历史状态混淆

### 2.3 后续遗忘

事实最开始存在，但在后续：

- 合并
- 压缩
- 更新
- 删除
- 去重

过程中消失。

### 2.4 错误更新

旧状态和新状态同时存在时，系统：

- 删除了正确状态
- 保留了错误状态
- 把历史状态当成当前状态
- 把新旧事实错误合并

处理方式：

```text
优化 Write Skill
```

其中：

- 初始遗漏、写错主要修 Extract / Capture Skill；
- 后续遗忘、错误更新主要修 Update / Consolidation Skill。

---

## Type 3：记忆和检索都正确，但回答仍然错误

判断条件：

```text
retrieved memories 已经包含充分证据
但回答模型仍然答错
```

这一类更接近回答模型能力问题。

当前工作暂时不重点处理，但需要单独记录，避免错误归因到 Read 或 Write Skill。

---

## 6. Skill Bank 的存储结构

每条 Skill 建议包含以下字段：

```json
{
  "skill_id": "read_current_state_v2",
  "module": "read",
  "summary": "处理当前状态和最新状态问题",
  "trigger": "问题询问 current、latest、now 或 updated 状态",
  "negative_trigger": "问题明确询问某个历史时间点",
  "action": [
    "检索当前值和历史值",
    "增加时间条件",
    "按时间重新排序"
  ],
  "keywords": [
    "current",
    "latest",
    "updated",
    "now"
  ],
  "origin_failure_types": [
    "knowledge-update",
    "temporal-reasoning"
  ],
  "validation": {
    "fixed": 7,
    "regressions": 0
  }
}
```

Skill Bank 需要支持：

```text
ADD
UPDATE
MERGE
SPLIT
DISABLE
```

不能只不断追加，否则 Skill 会越来越重复和冲突。

---

## 7. Read Skill 的优化流程

Type 1 的处理流程建议拆成三层。

### 7.1 Skill 没有被调用

检查：

- Skill summary 是否准确
- Skill 关键词是否充分
- Skill embedding 是否容易匹配
- Trigger 是否太窄
- 是否需要增加正例和反例

处理：

```text
修改 Skill 的 summary、trigger、keywords
```

---

### 7.2 Skill 被调用，但没有正确执行

检查：

- Reader Agent 是否按照 Skill 生成 query plan
- 是否真正加入时间、实体或关键词条件
- 是否执行多路检索
- 是否使用了 Skill 指定的检索深度

处理：

```text
修改 Skill 的 action 和输出格式约束
```

---

### 7.3 Skill 被执行，但仍然没有答对

检查：

- Query 是否仍然不完整
- 是否缺少其他证据
- Top-k 是否过小
- Rerank 是否错误
- 完整 MemDB 中是否确实存在答案所需事实

如果完整 MemDB 中没有充分事实，则重新归类为 Type 2。

---

## 8. Write Skill 的优化流程

Type 2 的优化不能只看最终 MemDB。

因为事实可能：

```text
最初正确写入
→ 后续更新
→ 被删除或错误合并
```

因此需要保留记忆生命周期中的状态。

推荐保存：

```text
每个 session 或每次 update 后的 MemDB 快照
```

例如：

```text
M0：初始状态
M1：处理 Session 1 后
M2：处理 Session 2 后
M3：处理 Session 3 后
...
MT：最终 MemDB
```

然后对某个事实逐步检查：

```text
第一次出现在哪个 session
什么时候首次进入 MemDB
什么时候首次消失或发生错误变化
```

由此可以区分：

```text
从未写入
vs
写入后被遗忘
```

---

## 9. 使用空间换时间

为了降低重复计算成本，可以缓存所有重要中间结果。

建议保存：

### 9.1 MemDB 快照

```text
history_id
writer_skill_version
session/update_step
MemDB 内容
```

### 9.2 QA 运行记录

```text
question_id
MemDB version
read_skill version
called_skill_ids
retrieved memories
model answer
judge result
failure type
```

### 9.3 Skill 版本

```text
Read Skill Bank R0, R1, R2...
Write Skill Bank W0, W1, W2...
```

这样：

- Type 1 优化时不需要重新构建 MemDB；
- 不同 Read Skill 可以反复复用同一个 MemDB；
- Type 2 可以定位事实在哪个阶段丢失；
- 可以比较不同 Skill 版本的效果。

---

## 10. 错误池与迭代流程

系统维护三个池。

## 10.1 Error Pool

保存当前未修复的问题。

包括：

```text
Type 1
Type 2
暂未解决的问题
```

## 10.2 Replay Pool

保存原本已经答对的问题。

用途：

```text
检查新 Skill 是否破坏原本正常的问题
```

## 10.3 Validation Pool

保存不参与 Skill 生成的问题。

用途：

```text
检查 Skill 是否具有跨问题和跨对话的泛化能力
```

---

## 11. Read Skill 优先处理

整体顺序建议为：

```text
先处理所有 Type 1
→ 得到稳定 Read Skill
→ 用新 Read Skill 重跑全部错误池
→ 重新诊断剩余错误
→ 再处理 Type 2
```

原因：

- Type 1 不需要重建 MemDB；
- 成本较低；
- 一部分看似 Type 2 的问题可能只是检索失败；
- Read Skill 稳定后，剩余 Type 2 更可信。

---

## 12. 单个问题的 Skill 优化循环

对单个问题设置最大迭代次数，例如：

```text
J = 3
```

流程：

```text
问题失败
→ 诊断原因
→ 生成 candidate skill
→ 检查 Skill 是否被调用
→ 检查 Skill 是否被执行
→ 检查答案是否修复
→ 检查其他问题是否回退
```

可能的结果：

```text
SUCCESS
FAILED_MAX_ITER
REGRESSION_REJECTED
RECLASSIFIED
UNRESOLVED
```

单题答对不能直接把 Skill 加入稳定 Skill Bank。

还需要在：

- 同类型错误问题
- Replay Pool
- Validation Pool

上检查。

---

## 13. Candidate Skill 与 Stable Skill

建议将 Skill Bank 分成两层。

```text
Candidate Skill Bank
Stable Skill Bank
```

### Candidate Skill

刚从某个失败中产生，尚未经过充分验证。

### Stable Skill

满足以下条件后才进入：

- 能修复目标问题
- 能改善同类问题
- 不明显破坏原本正确的问题
- 能被稳定触发
- 不显著增加 token
- 不与已有 Skill 冲突

---

## 14. Skill 初始化方案

### 方案一：空 Skill Bank

初始没有具体 Skill，只规定：

- Skill 的字段结构
- ADD / UPDATE / MERGE / DISABLE 规则
- Skill 不能包含具体问题答案
- Skill 必须可以跨样本复用

优点：

- Skill 的增长更自然
- 可以观察 Skill 如何从失败中形成
- “自发涌现”更明显

缺点：

- 前期不稳定
- 容易产生重复 Skill
- 对 Skill 管理能力要求高

---

### 方案二：提供 S0

先根据 baseline 的真实错误，总结出一版初始 Skill Bank：

```text
S0
```

然后继续迭代优化。

优点：

- 更稳定
- 更容易跑通
- 前期错误更少

缺点：

- 需要说明 S0 的来源
- 不能只比较 Baseline 和最终方法
- 必须比较：

```text
Baseline + S0
vs
Baseline + S0 + Skill Evolution
```

这样才能证明提升来自后续迭代，而不是初始规则。

---

### 推荐做法

主实验可以使用：

```text
S0 + Skill Evolution
```

保证系统稳定。

同时增加一个消融实验：

```text
Empty Skill Bank + Skill Evolution
```

用于观察 Skill 是否能够从空状态逐步形成。

---

## 15. 推荐的实验组

```text
1. Baseline
2. Baseline + S0
3. Baseline + Empty Skill Induction
4. Baseline + S0 + Skill Evolution
```

重点比较：

```text
Baseline + S0
vs
Baseline + S0 + Skill Evolution
```

证明迭代优化的价值。

---

## 16. 最终技术路径

```text
第一步：构造普通记忆
- 使用 baseline memory system
- 保存每个 history 的 MemDB
- 保存 session/update-level 快照

第二步：运行伪 QA
- 使用构造好的多 QA 数据
- 保存检索结果、Skill 调用路径和回答结果

第三步：形成错误池
- 判断 Type 1、Type 2、Type 3
- 优先处理 Type 1

第四步：优化 Read Skill
- 检查是否被调用
- 检查是否被正确执行
- 检查是否真正修复
- 检查是否引起回退

第五步：重跑错误池
- 使用稳定 Read Skill
- 重新诊断剩余问题

第六步：优化 Write Skill
- 定位事实第一次出现的位置
- 检查事实是否首次写入
- 检查事实在哪个 update 阶段丢失
- 分别修改 Capture Skill 或 Update Skill

第七步：阶段性重建
- 使用新 Write Skill 重建部分或全部 MemDB
- 保存新版本
- 重跑 QA

第八步：冻结 Skill Bank
- 得到最终 Read Skill Bank
- 得到最终 Write Skill Bank

第九步：正式测试
- 在未见过的长对话上构建全新 MemDB
- 使用冻结 Skill Bank
- 使用官方 QA 评测
```

---

## 17. 最终定位

整个系统可以简单描述为：

> Memory in Memory 为普通记忆系统增加一层关于记忆操作经验的长期记忆。系统从历史问答失败中学习如何更好地构建、更新和检索记忆，并将成功经验保存为可检索、可执行和可更新的 Skill。

一句话区分：

```text
普通 Memory：保存用户历史
Meta-Memory：保存记忆系统自己的经验
Skill：Meta-Memory 的可执行形式
```

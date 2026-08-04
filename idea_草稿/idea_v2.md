# Memory in Memory：当前技术方案

## 1. 整体目标

系统主要处理两类问题：

1. 记忆已经存在于 MemDB，但没有被正确检索；
2. 原始对话中有信息，但没有被正确写入或保留到 MemDB。

对应地，系统维护两套独立的 Skill：

```text
Read Skill Bank  → 给 Memory Reader Agent 使用
Write Skill Bank → 给 Memory Writer Agent 使用
```

两套 Skill Bank 初始都为空，只给定统一的 Skill 结构、检索方式和增删改并工具。

---

## 2. 主体架构

```text
Raw Conversation
      │
      ▼
Memory Writer Agent
      │
      ▼
MemDB
      │
      ▼
Memory Reader Agent
      │
      ▼
Answer Model
```

当回答错误时，进入反馈学习流程：

```text
Question + Gold Answer + Model Answer
                  │
                  ▼
              Judge-Agent
                  │
          ┌───────┴────────┐
          ▼                ▼
      Type1 Task        Type2 Task
          │                │
          ▼                ▼
   Read Meta-Agent    Write Meta-Agent
          │                │
          ▼                ▼
   Read Skill Bank    Write Skill Bank
```

模块职责：

- `Judge-Agent`：判断答案是否正确，并定位错误类型；
- `Read Meta-Agent`：根据 Type1 错误生成和修改 Read Skill；
- `Write Meta-Agent`：根据 Type2 错误生成和修改 Write Skill；
- `Skill Manager`：负责 Skill 的检索、增删改并、状态切换和轨迹保存；
- `Reader/Writer Agent`：负责真正执行 Skill。

---

## 3. Judge-Agent 的诊断流程

Judge-Agent 的输入包括：

```text
question
gold answer
model answer
正常检索结果
正常检索关键词
已调用的 Skill
```

Judge-Agent 还可以调用工具：

```text
搜索完整 MemDB
读取 MemDB 条目
搜索 raw conversation
读取某个 session
读取 MemDB checkpoint
比较前后 MemDB
```

### 第一步：判断答案

```text
正确
部分正确
错误
```

如果正确，当前问题结束。

如果部分正确或错误，则把 gold answer 拆成若干必要事实，再逐个判断。

---

## 4. Type1：记忆存在，但没有检索出来

### 定义

```text
目标事实存在于当前完整 MemDB，
但正常 Reader 没有把它检索出来。
```

Judge-Agent 会对完整 MemDB 做一次高召回搜索。这个搜索主要用于诊断，不以在线成本为主要考虑。

可以使用：

```text
多组语义查询
关键词查询
实体查询
时间条件
全量候选重排
LLM 语义核验
```

如果目标事实能在完整 MemDB 中找到，但不在正常检索结果中，则生成 Type1 Task。

### Type1 Task 保存的内容

```text
question
gold fact
正常检索关键词
正常检索结果
Oracle 检索关键词
Oracle 找到的 MemDB 条目
当前已调用的 Read Skill
当前执行轨迹
```

### Type1 修复目标

```text
让正常 Reader 能检索到当前 MemDB 中已经存在的相关信息。
```

即使 MemDB 本身还缺其他事实，只要现有信息已经被正确检索，Type1 就算修复完成。

---

## 5. Type2：原始对话中有，但 MemDB 没有正确保留

### 定义

```text
raw conversation 中存在目标事实，
但当前 MemDB 中没有，或者内容已经被写错、覆盖或丢失。
```

Judge-Agent 会先在 raw conversation 中定位该事实出现的位置，再检查对应的 MemDB checkpoint。

常见情况包括：

```text
首次没有写入
首次写入时写错
后续更新时丢失
后续更新时错误覆盖或合并
```

### Type2 Task 保存的内容

```text
gold fact
raw conversation 证据
出问题前的 MemDB
出问题后的 MemDB
相关 MemDB 条目
完整 MemDB
当时调用的 Write Skill
写入或更新轨迹
```

### Type2 修复目标

```text
让 raw conversation 中的信息被正确写入 MemDB，
并在后续更新中保持正确。
```

修复时从出问题前的 checkpoint 开始重放，不需要每次都从整个 history 开头重建。

---

## 6. Error Pool

所有错误任务进入统一错误池。

每个错误任务相互独立，但共享同一个 Skill Bank。

一个问题可以同时包含：

```text
某个事实是 Type1
另一个事实是 Type2
```

处理顺序：

```text
先处理 Type1
再处理 Type2
```

一个 epoch 内不反复在 Type1 和 Type2 之间跳转。

Type2 修复后可能产生新的 Type1，这类影响放到下一 epoch 统一发现。

---

## 7. Skill 的基本结构

Read Skill 和 Write Skill 使用相同结构：

```json
{
  "skill_name": "...",
  "skill_abstract": "...",
  "action": [
    "...",
    "..."
  ]
}
```

### skill_name

- 在对应 Skill Bank 中唯一；
- 简短；
- 不包含具体问题、用户或答案。

### skill_abstract

需要说明：

- 什么情况下适用；
- 解决什么问题；
- 普通处理为什么容易失败；
- 大致应该怎么解决。

它主要用于 Skill 检索。

### action

可以是：

- 一套操作流程；
- 一组执行建议；
- 对 Reader 或 Writer 的具体提醒。

---

## 8. Skill 不直接提前提供给 Meta-Agent

处理一个错误任务时，不把整个 Skill Bank 塞给 Meta-Agent。

流程：

```text
错误任务
   ↓
Meta-Agent 先独立生成一个 draft skill
   ↓
根据错误任务和 draft skill 检索现有 Skill
   ↓
返回 Top-k 候选
   ↓
Meta-Agent 决定增删改并
```

需要区分：

```text
draft skill：临时生成，还没有进入 Skill Bank
candidate：经过操作后，真正进入 Skill Bank 等待验证
```

---

## 9. Skill 检索

Meta-Agent 可以调用：

```text
skill.search(...)
skill.get(name)
skill.trace(name)
```

### 搜索输入

使用两类查询：

```text
任务查询：
当前错误、失败轨迹、修复目标

Draft 查询：
draft skill 的 name 和 abstract
```

然后进行混合检索：

```text
Embedding 检索
+
BM25 / 关键词检索
+
简单上下文匹配
```

最后融合得到大约 Top-10 的候选 Skill。

粗召回主要使用：

```text
skill_name + skill_abstract
```

只有当 Meta-Agent 判断某个 Skill 可能相关时，再读取完整 action。

---

## 10. Meta-Agent 的操作

Meta-Agent 可以选择：

```text
REUSE
ADD
UPDATE
MERGE
DELETE
```

### REUSE

现有 Skill 已经足够，只是之前没有被正确调用或执行。

### ADD

现有 Skill 都不能覆盖当前经验，直接把 draft skill 变成 candidate。

### UPDATE

某个现有 Skill 基本相关，但 abstract 或 action 不完整。

### MERGE

多个现有 Skill 分别覆盖一部分，或者已经明显重复。

### DELETE

某个 Skill 持续错误、重复或已经被其他 Skill 替代。

---

## 11. Skill 状态

系统维护三种状态：

```text
candidate
use
frozen
```

### candidate

当前正在验证的新 Skill，正常参与检索和执行。

### use

已经提交并正常使用的 Skill。

### frozen

旧版本、失败版本、被合并版本或待删除版本。

不参与正常检索，但保留给 Meta-Agent 查看。

---

## 12. Skill 的演化过程

每次修改都生成新版本，不直接覆盖旧版本。

```text
S0: use
   ↓ 修改 abstract
S1: candidate
   ↓ 仍然失败
S1: frozen
   ↓ 修改 action
S2: candidate
```

下一轮 Meta-Agent 可以看到：

```text
当前 candidate
当前 frozen
candidate 是怎么从 frozen 产生的
之前的 Meta-Agent 决策
每轮执行结果
```

---

## 13. 处理任务前的 Preflight

错误池中的任务可能因为别的任务修改了 Skill 而自动被修好。

因此每个任务正式处理前，先用当前 Skill Bank 轻量重跑一次。

### 已经修好

```text
标记为 auto_resolved
不再进入 Meta-Agent
```

### 仍然失败

```text
重新检索当前相关 Skill
再交给 Meta-Agent
```

错误任务不永久绑定某个 Skill 版本。

---

## 14. Candidate 的验证

Candidate 需要经过两次测试。

### 强制调用

直接把 candidate 给 Reader 或 Writer。

目的：

```text
判断 Skill 内容本身有没有用。
```

### 自然调用

恢复正常 Skill 检索。

目的：

```text
判断 Skill 能不能被正常找到和执行。
```

需要记录：

```text
retrieved
selected
executed
effective
```

---

## 15. Candidate 必须被引用

为修复某个错误生成的 candidate，在下一次回放中必须：

```text
被检索
被选择
被执行
```

或者由功能等价的合并后 Skill 替代。

如果没有进入 Top-k，优先修改 abstract 或检索方式。

如果已经执行但仍失败，优先修改 action 或重新考虑是否应该合并。

---

## 16. Skill 修改冲突

多个错误任务共享 Skill Bank，因此一个 Skill 被修改后，其他任务可能：

```text
已经被自动修好
仍然失败，但相关 Skill 已变化
原来引用的 Skill 已被合并或删除
```

处理方式：

```text
错误任务只保存稳定的失败证据
Skill Manager 保存 Skill 变化
任务处理前重新检查当前 Skill Bank
完整轨迹按需读取
```

如果 Skill 被合并，可维护轻量映射：

```text
旧 Skill A → 新 Skill C
旧 Skill B → 新 Skill C
```

如果 Skill 被删除，则旧引用直接触发重新检索。

---

## 17. Skill 工具

### 读取工具

```text
skill.search(queries, top_k)
skill.get(skill_name)
skill.trace(skill_name)
```

### 修改工具

```text
skill.add(skill_name, skill_abstract, action)

skill.polish_abstract(skill_name, new_abstract)

skill.action.append(skill_name, item)
skill.action.update(skill_name, index, item)
skill.action.delete(skill_name, index)
skill.action.replace(skill_name, action_list)

skill.merge(
  source_skill_names,
  new_skill_name,
  new_skill_abstract,
  new_action
)

skill.delete(skill_name)
```

状态切换由 Skill Manager 自动完成，Meta-Agent 不直接修改 candidate、use、frozen。

---

## 18. 单个错误任务的完整流程

```text
取出错误任务
    ↓
Preflight：使用当前 Skill Bank 重跑
    ├── 已修好 → auto_resolved
    └── 仍失败
          ↓
Meta-Agent 独立生成 draft skill
          ↓
任务查询 + draft 查询
          ↓
检索 Top-k 现有 Skill
          ↓
Meta-Agent 决定：
REUSE / ADD / UPDATE / MERGE / DELETE
          ↓
生成 candidate
          ↓
强制调用测试
          ↓
自然调用测试
          ↓
成功或达到最大迭代次数
```

---

## 19. 达到最大迭代次数

如果达到最大迭代次数仍未完全修复：

```text
保留最新 candidate
candidate → use
标记 unresolved
保存完整 repair trace
```

不回滚到最初版本。

后续遇到相似错误时，可以基于：

```text
当前 use Skill
历史 repair trace
新的失败轨迹
```

继续优化。

---

## 20. 一个 Epoch

### Epoch 开始

固定当前：

```text
MemDB
Read Skill Bank
Write Skill Bank
```

运行全部伪 QA。

### 统一诊断

Judge-Agent 生成：

```text
Type1 Task Pool
Type2 Task Pool
Type3 / Invalid Pool
```

### Phase A：处理 Type1

```text
固定 MemDB
逐个修复 Read Skill
```

### Phase B：处理 Type2

```text
逐个修复 Write Skill
从对应 checkpoint 重放
更新相关 MemDB 分支
```

Type2 错误任务不强制按时间排序，视为独立案例处理。

### Epoch 结束

保存：

```text
新的 Read Skill Bank
新的 Write Skill Bank
新的 MemDB 版本
Repair Trace
QA Logs
Unresolved Tasks
```

下一 epoch 使用新版本重新运行全部 QA，并统一发现新的跨任务和跨类型影响。

---

## 21. 初始状态

```text
Read Skill Bank = []
Write Skill Bank = []
```

只预先定义：

```text
Skill Schema
Skill 检索工具
Skill 增删改并工具
candidate / use / frozen 状态
Judge-Agent 诊断工具
最大迭代次数
Epoch 流程
```

不预置任何具体 Skill。

---

## 22. 最终测试

训练结束后冻结：

```text
Final Read Skill Bank
Final Write Skill Bank
```

在未见过的 history 上：

```text
重新构建 MemDB
使用冻结 Skill 进行检索和写入
回答官方 QA
```

测试阶段：

```text
不向 Reader、Writer 或 Meta-Agent 提供 gold answer
不根据测试错误继续修改 Skill
不修改冻结后的 Skill Bank
```

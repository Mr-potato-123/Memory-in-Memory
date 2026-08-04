# 三类错误诊断逻辑报告（修订版）

## 1. 最终结论

目前应当明确区分三类错误：

1. **回答错误（Answer Failure）**：回答模型已经拿到了足够的信息，但仍然答错。
2. **检索错误（Access Failure）**：当前记忆中存在对回答有用的信息，但自然搜索链没有把它检索出来。
3. **构建错误（Cons Failure）**：回答所需的信息没有被当前记忆正确保存，需要沿构建历史定位第一次出错的位置。

三者不是同一个 Agent 内部的三个标签，也不是必须三选一的总分类器。正确工作流是：

```text
LLM-as-Judge 已确认回答错误（P/I）
                    │
                    ▼
            先检查 Answer Failure
                    │
                    ▼
          ┌─────────┴─────────┐
          │                   │
          ▼                   ▼
   Access Diagnosis     Cons Diagnosis
       并行                 并行
```

其中：

- Answer 只判断“拿到的信息已经够不够”；
- Access 只判断“当前已有的有用记忆是否都搜到了”；
- Cons 只判断“当前记忆本身是否缺失或错误，以及第一次在哪里构建错了”。

这比此前把 `ANSWER_FAILURE` 塞进 Access 内部作为三分支更清楚。Answer、Access、Cons 现在各自拥有独立问题、独立输入和独立输出。

---

## 2. Judge 与 Answer Failure 不是一回事

现有 LLM-as-Judge 的输入是：

```text
question
reference answer
runtime prediction
```

它只回答：

```text
这个回答对不对？
```

Judge 输出：

- `C`：正确；
- `P`：部分正确；
- `I`：错误。

Judge 不知道运行时检索到了什么，因此不能判断为什么错。

对于 Judge 标记为 `P/I` 的问题，才进入 Answer Diagnosis。Answer Diagnosis 额外读取运行时真正检索到并进入上下文的记忆，回答：

```text
这些检索结果是否已经足以支持标准答案？
```

如果足够而模型仍然答错，才判定：

```text
ANSWER_FAILURE
```

这里不让诊断模型重新回答问题，也不再调用旧的 `AnswerCheckAgent`。诊断模型只做信息充分性判断，避免因为“第二个模型也不会答”而错误归因。

---

## 3. 第一类：Answer Failure

### 3.1 输入

Answer Diagnosis 只能读取：

- 问题；
- 标准答案；
- 运行时模型答案；
- Judge 的 `P/I` 结果；
- 运行时每一次搜索和查看操作；
- 每一步实际返回给回答模型的记忆正文。

它不能读取：

- 没有被检索到的当前记忆；
- raw conversation；
- evidence 原文；
- 记忆构建候选和决策；
- 记忆版本修改历史；
- Access 或 Cons 的结果。

这是为了严格复原回答模型当时实际拥有的信息，不能事后把它没看到的信息补进去。

### 3.2 判断规则

先把标准答案拆成不可缺少的事实点，例如：

```text
标准答案：Caroline 与她的队友参加了比赛。

必要事实点：
1. Caroline 参加了比赛；
2. 她不是独自参加，而是和队友一起。
```

然后检查检索结果是否覆盖每一个必要事实点。

程序根据模型给出的事实覆盖关系计算：

```python
retrieved_context_sufficient = (
    所有必要事实点都有检索结果支持
    and 不存在无法消解的关键矛盾
)

answer_failure = (
    judge_label in {"P", "I"}
    and retrieved_context_sufficient
)
```

因此，Answer Failure 的含义非常具体：

> 正确答案所需信息已经进入回答模型上下文，但原运行模型仍然没有正确使用这些信息。

### 3.3 输出

Answer Failure 只形成审计记录，例如：

```text
问题 ID
必要事实点
每个事实点对应的已检索记忆
信息是否充分
判断原因
置信度
```

它：

- 不生成修复包；
- 不进入 Skill-Maker；
- 不生成检索词；
- 不修改回答提示词；
- 不阻止后面的 Access 和 Cons。

如果检索到的信息不够，只能得到 `NO_ANSWER_FAILURE`。这并不自动证明是 Access 或 Cons，需要继续执行后两条诊断。

---

## 4. 第二类：Access Failure

### 4.1 它真正要回答的问题

Access 只回答：

> 当前记忆中已经存在、并且对标准答案有帮助的信息，是否全部被原始自然搜索链检索到了？

它比较两个集合：

```text
当前相关记忆中的有用条目
          与
运行时实际检索到的当前记忆条目
```

只要存在一条当前有用记忆没有被检索到，就是 Access Failure。

### 4.2 Access 只看当前记忆

这是对旧报告 4.2、4.3 的关键修正。

Access 允许读取：

- 问题；
- 标准答案；
- 冻结时刻的当前相关记忆条目；
- 自然搜索链每一步返回的当前记忆条目；
- 搜索步骤及条目 ID。

Access 严禁读取：

- raw conversation；
- evidence 原文；
- 旧版本记忆；
- 版本链；
- 父版本；
- before/after；
- 构建候选和决策；
- Cons 诊断结果。

因此，Access 完全没有理由参考“其他版本记忆”。历史版本只能用于回答“信息是什么时候被改坏的”，这是 Cons 的职责，不是 Access 的职责。

### 4.3 evidence 反向查找不等于开放版本历史

程序仍需根据 LOCOMO 标注的 evidence message ID 找到相关记忆，但这只是数据库中的定点查找：

```text
evidence message ID
        ↓ 程序查询
冻结快照中仍处于 active 状态的当前记忆条目
        ↓
交给 Access
```

Access 模型最终只看到当前条目。

它不会看到：

```text
这个条目以前是什么
经过多少次 UPDATE
由哪些父版本合并而来
哪个 commit 改过它
```

如果运行时的 `inspect_memory` 工具意外返回了历史版本：

- Answer Diagnosis 可以保留，因为回答模型当时确实看到了；
- Access 输入必须过滤掉历史版本，只保留冻结快照的当前版本。

### 4.4 Access 如何判断

模型负责判断哪些当前条目在语义上支持标准答案的必要事实点。

程序负责集合运算：

```python
useful_current_ids = 当前记忆中对答案有帮助的条目ID集合
retrieved_current_ids = 整条自然搜索链返回过的当前条目ID集合
missing_useful_ids = useful_current_ids - retrieved_current_ids
access_failure = bool(missing_useful_ids)
```

这里不要求“当前记忆已经足以完整回答问题”。

例如：

- 当前记忆缺失事实 A，说明可能有 Cons Failure；
- 当前记忆仍保存事实 B；
- 运行时连事实 B 也没搜出来；

那么同一个问题可以同时存在：

```text
ACCESS_FAILURE：事实 B 没搜到
CONS_FAILURE：事实 A 没存好
```

两边分别修复，不需要建立“两个问题同时存在”的合并标签。

### 4.5 Access 修复包

Access Failure 包只保留：

- 问题和标准答案；
- 当前有用记忆；
- 实际检索到的当前记忆；
- 漏掉的当前记忆；
- 每一步原始搜索结果；
- 为什么漏掉的条目对答案有用。

不要求诊断模型给出：

- 新检索语句；
- 关键词；
- 过滤条件；
- BM25/语义权重；
- 检索深度；
- 修复后的提示词。

这些属于后续 Skill-Maker 的工作。

---

## 5. 第三类：Cons Failure

### 5.1 第一阶段先判断当前记忆是否有问题

Cons 的第一阶段只读取：

- 问题；
- 标准答案；
- 冻结快照中的当前相关记忆条目。

它明确不能读取：

- 运行时检索步骤；
- 检索到的记忆集合；
- Access 结果；
- Answer 结果；
- 运行时模型答案。

第一阶段先回答：

> 当前相关记忆是否完整、正确地保存了标准答案所需信息？

对于每一个必要事实点，输出：

```text
FULL       完整保存
PARTIAL    只保存一部分
MISSING    完全缺失
INCORRECT  保存错误
```

如果全部为 `FULL`，输出 `NO_CONS_FAILURE`，不再读取历史。

如果存在 `PARTIAL/MISSING/INCORRECT`，才进入第二阶段。

### 5.2 第二阶段才开放 raw conversation 和历史

当第一阶段确认当前记忆存在构建问题后，程序才向 Cons 第二阶段提供：

- 标注的 raw evidence message ID 和原文；
- 涉及的当前记忆条目；
- 抽取候选；
- ADD/UPDATE/SKIP 等构建决策；
- commit 和 change；
- 父版本；
- 每次修改前后的记忆正文。

然后严格按时间从头检查：

```text
raw evidence 是否存在
        ↓
是否生成正确候选
        ↓
是否做出正确 ADD / UPDATE / SKIP 决策
        ↓
第一次落盘时是否正确
        ↓
后续哪一次 UPDATE / DELETE / MERGE 首次破坏信息
```

### 5.3 只定位第一个错误

如果一条信息先在 commit 8 被错误改写，随后又在 commit 12 被删除，Cons 只把 commit 8 作为修复目标。

最终包必须包含：

- 出问题的标准答案事实；
- 对应 raw evidence message ID；
- 涉及的 memory ID；
- 最早错误阶段；
- commit/change/operation；
- 修改前版本；
- 修改后版本；
- 一段无需查看数据库也能理解的中文式自然语言原因（机器产物本身用英文）。

原因应当明确口述：

```text
原始对话表达了什么；
正确记忆应该保留什么；
哪一步首次漏掉或改坏；
修改前后发生了什么变化；
为什么这一变化会导致当前记忆无法支持答案。
```

如果 raw evidence 本身不支持标准答案，应标记为数据问题并交给人工复核，不能虚构一个 Cons Failure。

---

## 6. 三类诊断的权限总表

| 数据 | Answer | Access | Cons 第一阶段 | Cons 第二阶段 |
|---|---:|---:|---:|---:|
| 问题 | 是 | 是 | 是 | 是 |
| 标准答案 | 是 | 是 | 是 | 是 |
| 运行时模型答案 | 是 | 否 | 否 | 否 |
| 实际检索结果 | 是，完整复原 | 是，仅当前版本 | 否 | 否 |
| 未被检索的当前相关记忆 | 否 | 是 | 是 | 是 |
| raw evidence | 否 | 否 | 否 | 是 |
| 历史记忆版本 | 否 | 否 | 否 | 是 |
| 构建候选和决策 | 否 | 否 | 否 | 是 |
| 其他诊断结果 | 否 | 否 | 否 | 否 |

这张表就是工程实现时最重要的隔离契约。

---

## 7. 三者能否同时出现

这三种结果不需要强行三选一。

### Answer 与 Access

若标准答案所需信息已经全部被检索到，通常：

```text
ANSWER_FAILURE = true
ACCESS_FAILURE = false
```

若关键信息没有检索到：

```text
ANSWER_FAILURE = false
ACCESS_FAILURE = true
```

但不应使用一条结果硬编码覆盖另一条，仍由各自证据独立计算。

### Access 与 Cons

它们可以同时存在：

```text
当前记忆丢失了事实 A          -> Cons Failure
当前仍保存的事实 B 没有搜到   -> Access Failure
```

此时形成两个独立修复包，各自进入自己的后续修复流程。不要创建：

```text
both_failures
combined_failure
overall_failure_type
```

### Answer 与 Cons

若检索结果已经足以回答标准答案，但当前其他相关记忆仍有构建问题，两项理论上也可能同时被记录。诊断系统不应为了让标签互斥而隐藏一个有证据支持的问题。

---

## 8. 模型与纯算法各做什么

### 交给模型

- 把标准答案拆成必要事实点；
- 判断一段记忆是否在语义上支持事实点；
- 判断检索结果整体是否足以回答；
- 判断当前记忆是否缺失、片面或错误；
- 解释最早构建错误及其影响。

### 交给程序

- 按 evidence ID 查询当前 active 记忆；
- 读取并整理自然搜索链；
- 过滤 Access 输入中的历史版本；
- 对 ID 做并集和差集；
- 验证模型返回的 ID；
- 按 commit 排序构建历史；
- 补全父版本和 before/after；
- 选择最早的已验证错误；
- 路由输出、保存进度和独立恢复。

原则是：

> 语义是否支持交给模型；ID、集合、顺序、版本关系和输出路由交给程序。

---

## 9. 物理目录

建议使用：

```text
outputs/diagnosis/deepseek_v4_pro_diag_v3/
├── answer_failure/
│   ├── answer_failures.jsonl
│   ├── progress.jsonl
│   ├── errors.jsonl
│   ├── summary.json
│   └── manifest.json
├── access_failure/
│   ├── packages/<conversation-id>/<qa-id>_access_failure.json
│   ├── progress.jsonl
│   ├── errors.jsonl
│   ├── summary.json
│   └── manifest.json
└── cons_failure/
    ├── packages/<conversation-id>/<qa-id>_cons_failure.json
    ├── progress.jsonl
    ├── errors.jsonl
    ├── summary.json
    └── manifest.json
```

其中：

- Answer 没有 `packages`，因为它只记录；
- Access 和 Cons 的包物理隔离；
- 三者进度、错误、汇总和恢复状态均独立；
- 不建立 combined 目录。

---

## 10. 并发方式

第一阶段先并发执行 Answer：

```text
Answer workers = 4
```

Answer 批次完成后，同时启动：

```text
Access workers = 4
Cons workers   = 4
最大同时模型调用数 = 8
```

Cons 对同一个问题先执行第一阶段；只有发现构建问题时，才在同一个 worker 中顺序执行第二阶段。不要在 Cons 内部再开嵌套线程池。

同一个 API key 可以公用，但每次调用必须使用全新消息上下文。共享密钥不等于共享上下文。

---

## 11. 对旧 4.2、4.3 的最终纠正

旧表述的问题有两个：

1. 把 Answer Failure 当成 Access 内部的一种结果，使 Access 同时回答“有没有搜全”和“模型会不会答”两个问题；
2. 为了判断 Access，引入了 lineage 和历史版本，扩大了不必要的权限。

现在明确改为：

```text
Answer：
只看实际检索结果，判断信息够但仍答错。

Access：
只看当前相关记忆和实际检索到的当前记忆，判断有用信息是否漏搜。

Cons：
先看当前记忆是否有问题；确认有问题后，才读取 raw evidence 和历史版本定位第一次错误。
```

因此，新版与本轮要求一致。Access 不再以任何形式参考其他版本记忆；版本历史只在 Cons 第二阶段按需开放。

---

## 12. 最终验收条件

实现完成时必须满足：

- Judge 为 `C` 的问题不进入诊断；
- 每个 `P/I` 问题先产生 Answer 记录；
- Answer 不重新回答问题、不生成修复包；
- Answer 完成后 Access 与 Cons 并行；
- Access 不读取 raw conversation；
- Access 不读取任何旧版本和版本链；
- Access 只比较当前有用记忆与检索到的当前记忆；
- Cons 不读取任何搜索或检索结果；
- Cons 第一阶段只看当前记忆和标准答案；
- 只有 Cons 候选才进入 raw/history 定位阶段；
- Cons 只输出最早的一个构建错误；
- Cons 原因能够口述清楚“原文—应保存内容—首次错误—影响”；
- Access 和 Cons 可以各自独立报告；
- 不产生合并标签或合并包；
- 三类诊断使用独立上下文和独立输出状态；
- 所有 AI/Claude 提示词与机器产物使用英文；
- 不运行 Skill-Maker。

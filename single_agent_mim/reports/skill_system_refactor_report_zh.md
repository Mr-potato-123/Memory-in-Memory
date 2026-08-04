# Skill 系统全局重构报告

## 1. 重构结果

本次重构已经把 Runtime 正式 Skill、Diagnosis 看到的 Skill 记录、候选 Skill 和批量 CRUD 分成清晰的四层：

```text
正式 Skill Bank
  ↓ Runtime 只读召回
Access / Construction 运行并记录 skill_trace
  ↓
Diagnosis 在诊断包中披露选中 Skill 与未选中近邻
  ↓
Candidate Skill Agent 生成候选或判断无需修改
  ↓ 候选物理隔离保存
聚类、统一召回、批量 CRUD、冲突重规划
  ↓
一次事务发布新的正式 Skill Bank
```

旧的“每发现一个错误就重放、修改并立即发布一个 Bank 版本”的 Skill-Maker 已从活动代码中移除。

## 2. Runtime Skill 召回

Access 与 Construction 只能检索正式 Bank：

- Access 使用完整问题作为 Skill 查询；
- Construction 使用完整会话的 `speaker: content` 文本作为 Skill 查询；
- 两个方向按照 `side` 严格过滤；
- 默认加载 Top-3；
- 额外披露之后 5 个没有加载的近邻。

Runtime 排序当前为：

```text
85% 语义相似度
15% 词法重合
```

每次召回记录：

- 正式 Bank 版本；
- 查询文本；
- 被加载 Skill 的完整快照、排名和分数；
- 没有加载的近邻 Skill；
- 语义分数和词法分数。

近邻 Skill 只进入 trace，不影响当前 Runtime Agent。

## 3. Diagnosis 变化

Access Diagnosis 包新增 Access `skill_trace`。其中包含当时真正加载的正式 Skill，以及排名靠后但没有加载的 Skill。

Construction Diagnosis 根据 gold evidence 的 message ID，反向找到处理这些消息的 Construction commit，再读取对应 Construction Skill trace。只有和证据消息相关的会话 Skill 记录进入诊断包。

Skill trace 不改变三类错误的判断原则：

- Answer 仍然只判断已经提供的上下文是否足够；
- Access 仍然只比较当前相关记忆与搜索链实际返回记忆；
- Construction 仍然从当前记忆开始筛查，并只定位第一个构建错误。

它的主要作用是让后续 Candidate Skill Agent 知道当时有哪些正式 Skill 被召回、哪些相似 Skill 没有被召回，避免重复学习。

## 4. 候选与正式 Skill 物理隔离

新的目录为：

```text
skills/
├── official/
│   ├── banks/
│   └── selected.json
├── candidates/
│   ├── access/
│   └── construction/
└── transactions/
```

约束如下：

- Runtime 只读取 `official/`；
- Candidate Agent 只写入 `candidates/access` 或
  `candidates/construction`；
- 候选文件无法被 Runtime 检索；
- 只有通过程序校验的批量事务才能产生正式 Bank 版本；
- 历史旧 Bank 会被复制到新目录，但不会自动删除。

## 5. Candidate Skill

候选的核心结构保持简短：

```json
{
  "name": "...",
  "description": "...",
  "content": ["..."],
  "solves": "一小段话，说明该 Skill 为了解决什么一般性问题。"
}
```

生成时 Candidate Agent 能看到完整诊断包和 Skill trace，并可以返回：

- 生成新候选；
- 正式 Bank 已经足够，无需生成；
- 当前错误不是 Skill 问题，无需生成。

`solves` 是诊断阶段和 CRUD 阶段之间的边界。CRUD 不再读取原始诊断包和 `skill_trace`。

## 6. 聚类与统一召回

Access 和 Construction 候选分别聚类。K-means 向量为：

```text
45% description embedding
35% content embedding
20% solves embedding
```

K-means 后再进行规则修正：

- 指向同一个正式 Skill 的候选归为同一组；
- BM25/词法高度重合的候选归为同一组；
- 普通规划批次最多 10 个候选。

每个类别都会计算完整的：

```text
candidate × official Skill 相似度矩阵
```

统一召回权重为：

```text
50% description 语义相似度
30% content 语义相似度
20% BM25
```

每个候选至少保留三个正式 Skill 邻居，候选主动声明的相关 Skill 强制加入，之后再加入覆盖整个批次的公共 Skill。

## 7. CRUD 与冲突处理

一个批次允许：

- 创建多个 Skill；
- 修改多个 Skill；
- 修改名称或 description；
- 增加、修改、删除、移动 content；
- 删除 Skill。

LLM 只输出操作计划，不直接修改 Bank。程序检查基础版本、目标 ID、候选覆盖、content 位置、旧内容和跨方向写入。

所有类别先基于同一个冻结正式 Bank 生成计划。若两个类别写入同一个 Skill：

1. 程序检测到写集合冲突；
2. 合并冲突类别；
3. 重新调用 CRUD Agent；
4. 得到一份统一修改计划。

全部冲突解除后，同一个方向的操作合并为一个发布事务。因此一轮整理中 Access 和 Construction 各最多产生一个新正式 Bank 版本。

## 8. 数据库兼容

SQLite 新增：

```text
access_runs.skill_trace_json
construction_commits.skill_trace_json
```

已有数据库打开时会自动检查字段并执行非破坏性迁移，不需要删除或重建原始记忆数据库。

## 9. 测试结果

已完成离线测试，未调用外部模型：

```text
50 passed
```

新增测试覆盖：

- 正式 Skill Top-k 与未加载近邻披露；
- 候选区与正式 Bank 的物理隔离；
- 空 Bank 下一个事务创建多个正式 Skill；
- Diagnosis 从 SQLite 读取持久化 Skill trace；
- 原有 Runtime、Diagnosis、记忆版本和评测流程兼容性。

## 10. 当前运行边界

这次重构完成的是完整工程链路和数据隔离。正式实验仍需下一轮真实运行验证：

- 新 Skill 是否自然进入 Top-k；
- 未进入 Top-k 时是否出现在披露近邻；
- Access/Construction 错误是否减少；
- 是否出现重复 Skill；
- Bank 大小、版本数量和修改振荡是否下降。

这些效果以新一轮真实 `skill_trace` 和 LLM-as-Judge 结果为准，不通过同一批错误反复修改到通过。

# Skill Bank 批量学习工作流

## 总体流程

```text
Runtime 正式 Skill 召回
  ↓
记录选中 Skill、未选中近邻、分数和 Bank 版本
  ↓
Diagnosis 输出诊断包并附带 skill_trace
  ↓
Candidate Agent 生成候选，或判断无需新增
  ↓
Access / Construction 候选分别保存
  ↓
候选聚类与类别修正
  ↓
计算 candidate × official Skill 相似度矩阵
  ↓
CRUD Agent 为每个类别输出多条操作
  ↓
检测跨类别写入冲突，合并后重新生成冲突计划
  ↓
每个方向一次事务发布正式 Bank
  ↓
下一轮通过真实 skill_trace 验证效果
```

## 1. Runtime 召回

- Access 使用完整问题召回 Access Skill。
- Construction 使用完整会话召回 Construction Skill。
- Runtime 只读取 `skills/official/`。
- Top-k Skill 进入 Agent 上下文。
- 排名紧随其后的若干 Skill 只写入 trace，不影响本次运行。

## 2. Candidate 生成

Candidate Agent 读取：

- 完整诊断包；
- 当时实际加载的 Skill；
- 没有加载的相似 Skill；
- 对应分数和正式 Bank 版本。

候选格式：

```json
{
  "name": "...",
  "description": "...",
  "content": ["..."],
  "solves": "一小段话，说明该 Skill 解决什么一般性问题。"
}
```

Agent 可以返回候选，也可以判断正式 Bank 已足够，或者该错误不是 Skill 问题。

## 3. 物理隔离

```text
skills/
├── official/
├── candidates/
│   ├── access/
│   └── construction/
└── transactions/
```

候选不能被 Runtime 检索。只有批量 CRUD 事务可以修改正式 Bank。

## 4. 聚类

Access 和 Construction 分别聚类：

```text
45% description embedding
35% content embedding
20% solves embedding
```

默认每类约 8 个候选，普通 CRUD 批次最多 10 个。K-means 后再根据共享正式 Skill 和词法重合修正类别。

## 5. 批次统一召回

不把多个候选拼成一个查询，而是计算完整矩阵：

```text
candidate × official Skill
```

权重为：

```text
50% description 语义相似度
30% content 语义相似度
20% BM25
```

每个候选至少保留三个正式 Skill 邻居，候选声明的相关 Skill 强制保留，再补充覆盖整个类别的公共 Skill。

## 6. CRUD

CRUD 不读取诊断包和 `skill_trace`，只读取：

- 候选 Skill；
- `solves`；
- 相似度关系；
- 相关正式 Skill。

一个类别可以创建多个 Skill，并输出多条操作：

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

LLM 只输出计划。程序检查基础版本、目标 ID、旧 content、候选覆盖和跨方向写入。

## 7. 冲突与发布

所有类别先基于同一个冻结正式 Bank 规划。两个类别写入同一个 Skill 时：

1. 程序检测冲突；
2. 合并冲突类别；
3. 重新调用 CRUD Agent；
4. 生成统一操作。

冲突解除后，每个方向的全部操作合并为一个发布事务。Access 与 Construction 每轮各最多产生一个正式 Bank 版本。

## 8. 验证

同一批次不反复修改到召回成功。最终效果在下一轮真实运行中验证：

- Skill 是否自然进入 Top-k；
- 是否只出现在未选中近邻；
- 是否真正减少 Access/Construction 错误；
- 是否控制重复 Skill、Bank 大小和版本数量。

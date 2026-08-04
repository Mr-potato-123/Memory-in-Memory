# Diagnosis V3 代码重构报告

## 1. 交付结论

三类诊断已经按最新讨论落成独立代码：

```text
Judge P/I
    ↓
Answer Diagnosis
    ↓
Access Diagnosis  ||  Cons Diagnosis
```

本次没有调用 DeepSeek API，也没有生成正式诊断结果。正式诊断和结果抽查交由 Claude 按英文运行指南执行。

## 2. 代码结构

```text
src/mim/
├── agents/
│   ├── answer_failure.py       # 判断检索信息足够但仍答错
│   ├── access_failure.py       # 判断当前有用记忆是否漏搜
│   └── cons_failure.py         # 当前记忆筛查 + 首错溯源
└── diagnosis/
    ├── schemas.py              # 三类诊断稳定数据契约
    ├── model_io.py             # JSON 调用、解析和 ID 校验
    ├── evidence.py             # 按权限生成三种数据视图
    ├── workflows.py            # 强制执行阶段和权限边界
    ├── artifacts.py            # 独立目录、原子写入和恢复记录
    └── runner.py               # 共享并发、重试和数据装配

scripts/
├── run_answer_failure.py
├── run_access_failure.py
└── run_cons_failure.py

prompts/diagnosis/
├── answer_failure.md
├── access_failure.md
├── cons_failure_stage_a.md
└── cons_failure_stage_b.md
```

三个脚本只是很薄的入口，数据加载、并发、恢复和错误处理集中在一个共享 runner 中，避免维护三份重复代码。

## 3. 三类诊断的实际权限

### Answer

读取：

- 问题、标准答案和运行时答案；
- Judge 结果；
- 原运行时完整自然搜索链。

它复原回答模型真正看到的信息，不重新回答问题。若每个必要事实都已经得到支持但原答案仍错，记录 `ANSWER_FAILURE`。

Answer 永远不生成修复包。

### Access

读取：

- 问题和标准答案；
- evidence ID 对应的当前 active 记忆；
- 原搜索链中返回的当前 active 记忆。

程序会删除：

- inspect 返回的旧版本；
- raw source；
- response 中其他历史信息；
- lineage、父版本和 before/after。

模型识别有用当前条目，程序计算：

```python
missing = useful_current_ids - retrieved_current_ids
```

只有 `missing` 非空才生成 Access 修复包。

### Cons

第一阶段只读取：

- 问题；
- 标准答案；
- 当前相关记忆。

如果所有必要事实都是 `FULL`，立即结束，不读取 raw conversation 和历史。

只有出现 `PARTIAL/MISSING/INCORRECT` 时，第二阶段才查询：

- raw evidence；
- candidates；
- decisions；
- commits/change events；
- before/after versions。

第二阶段只输出最早的一个构建错误。UPDATE 错误若没有经过验证的 before 和 after，会被判为无效模型输出。

## 4. 可维护性处理

- Schema、模型调用、数据查询、业务工作流、产物和批量调度已经分层。
- Agent 不直接查数据库，也不直接写文件。
- Workflow 决定什么时候允许读取哪类数据。
- Runner 每个线程持有自己的模型客户端，每个任务使用独立只读 SQLite 连接。
- 所有机器提示词和诊断产物字段均为英文。
- 输出由主线程统一写入，避免并发追加损坏 JSONL。
- 每个组件有独立 progress、errors、summary、manifest 和恢复状态。
- Answer 完成尝试后，Access 与 Cons 才可启动；Answer 的单项错误不会阻塞二者。
- Access 与 Cons 允许同时成立，但不会生成 combined 标签或目录。

旧 V2 runner 和 `FailureWorkflow` 暂时保留，是因为现有在线训练流程仍引用它们。文件头已经明确标为 Legacy；Diagnosis V3 的三个新入口完全不导入旧工作流或 `AnswerCheckAgent`。

## 5. 产物目录

```text
outputs/diagnosis/deepseek_v4_pro_diag_v3/
├── answer_failure/
│   ├── answer_failures.jsonl
│   ├── progress.jsonl
│   ├── errors.jsonl
│   ├── summary.json
│   └── manifest.json
├── access_failure/
│   ├── packages/
│   ├── progress.jsonl
│   ├── errors.jsonl
│   ├── summary.json
│   └── manifest.json
└── cons_failure/
    ├── packages/
    ├── progress.jsonl
    ├── errors.jsonl
    ├── summary.json
    └── manifest.json
```

## 6. 验证结果

全项目离线测试：

```text
47 passed
```

新增测试覆盖：

- 信息充足时 Answer Failure 只记录、不产包；
- Access 由集合差确定是否失败；
- Access 过滤 inspect 中的旧版本和 raw source；
- Cons 当前记忆完整时不会读取 raw/history；
- Cons 候选才进入第二阶段；
- Cons 只形成一个首错修复包。

使用真实 `conv-30` SQLite 做了只读数据烟雾检查：

```text
inspect 实际返回：v1 + v2
Access 得到：仅当前 v2
Access raw source 泄漏：false
Access 原始 response 泄漏：false
```

六个 train conversation 的新 Judge `P/I` 可执行任务共 688 条：

```text
conv-30   52
conv-42  157
conv-43  141
conv-44   82
conv-48  132
conv-49  124
```

## 7. Claude 交接

正式运行说明：

```text
docs/CLAUDE_RUN_DIAGNOSIS_V3.md
```

Claude 应先执行一个单条 smoke，再完整运行 Answer，最后并行启动 Access 和 Cons。不得调用旧 runner，不得运行 Skill-Maker，也不得复用旧诊断目录作为恢复状态。

# Memory in Memory（MiM）当前交接文档

更新时间：2026-08-14（Asia/Shanghai）  
仓库：`Memory_in_Memory/single_agent_mim`  
分支：`main`  
当前 HEAD：`c95b3c3 add split-level read-only access evaluator`

> 2026-08-14 架构转向：新实验以 Mem0 OSS 为事实记忆基座，MiM 聚焦 Skill
> 控制面。SQLite factual backend 暂时仅为历史实验兼容保留；最新边界与迁移
> 状态见 `docs/MEM0_PIVOT.md`。

## 0. 首要结论

MiM 不是一个新的事实记忆基座，也不以替代 Mem0、A-MEM、SimpleMem 为
目标。MiM 的目标是在任意 LLM 介导的记忆系统外增加一个可插拔的程序性
元记忆层：从失败轨迹中学习“以后应当如何构建、访问和使用记忆”，将经验
发布为可检索、可版本化、可 CRUD 的自然语言 Skill。

正式 Skill 只有两个 side：

- `construction`：指导如何从对话构建和更新对象级记忆；
- `access`：指导一道问题完整的记忆访问与回答策略。

每条正式 Skill 都只有统一的三字段结构：

```json
{
  "name": "Short human-readable name",
  "description": "When this Skill should be retrieved.",
  "content": ["One or more concise actionable instructions."]
}
```

Access Skill 内部不存在 `retrieval_skill`、`answer_skill`、`A1_skill`、
`A2_skill` 等类型。诊断可以定位失败发生在检索、证据使用或答案组合，但这
些是诊断元数据，不应成为正式 Bank 的内部切分。

当前最重要的架构问题是：运行时把统一 Access Skill 人为放进两个相互独立
的 A1/A2 prompt，并分别归因。这与原始的统一
`access + answer` 策略定义不一致，下一步应重构为“同一道题的一条连续、
受限轨迹”，而不是继续修补两个局部阶段。

## 1. 项目目的与研究定位

### 1.1 研究对象

对象级记忆回答“用户是谁、发生过什么、某状态何时有效”。MiM 的 Skill
记录的是程序性经验，例如：

- 相对时间应如何解析和保存；
- 状态变化时如何保留历史而不污染当前状态；
- 列表问题缺少组成项时如何改变查询；
- 多条证据如何核验主体、时间和关系后再组合；
- 如何避免把相似实体、相关事件或弱偏好当成直接答案。

因此，MiM 的核心贡献不是存储格式，而是：

1. 从错误答案向后定位 construction/access/answer 使用链上的失败机制；
2. 将可复用修复抽象为最小自然语言 Skill；
3. 通过候选隔离、聚类、CRUD、冲突检查和事务发布维护 Skill Bank；
4. 在未来问题中按可观察触发条件自然召回 Skill；
5. 通过完整轨迹区分“未召回、召回未采用、采用但无效、采用后回归”。

### 1.2 插件定位

`single_agent_mim` 是原生适配的最简研究基座，可以为了验证 MiM 卖点而
完整配合 Skill 注入。后续适配 Mem0、A-MEM、SimpleMem 等外部系统时，
MiM 应包装其已有的 memory add/search 接口，而不是要求外部基座改造成当前
SQLite schema。

最小插件边界应是：

```text
Construction adapter:
  session/messages + selected construction skills
  -> backend memory mutations + observable trace

Access adapter:
  question + backend search/inspect capability + selected access skills
  -> answer + evidence + one continuous access trajectory

Offline control plane:
  prediction + judge + traces + read-only evidence
  -> diagnosis -> candidates -> CRUD -> published bank
```

### 1.3 非目标

- 不靠堆确定性 case rule 变成另一个 LoCoMo 专用 SOTA；
- 不把训练问题的答案、人物、地点、日期写入 Skill；
- 不让 Skill 绕过证据或直接修改事实；
- 不要求每个外部基座复制 MiM 的内部 SQLite；
- 不用 test/val 反复训练或发布 Skill；Skill 学习应来自 train。

## 2. 数据集、split 与当前实验资产

配置：`configs/deepseek_v4_flash_fixed_topology.yaml`  
数据：`../LoCoMo/data/locomo10.json`  
冻结 split：`data/splits/locomo_swap_4_2_2.json`

| Split | Conversation | QA 数量 |
|---|---|---:|
| train | conv-30, conv-42, conv-43, conv-44, conv-47, conv-50 | 6 conversations |
| validation | conv-26 | 199 |
| validation | conv-41 | 193 |
| validation 合计 |  | 392 |
| test | conv-48 | 239 |
| test | conv-49 | 196 |
| test 合计 |  | 435 |
| val + test |  | 827 |

当前正式 Bank1：

```text
outputs/fullstack_v4p_20260813_bank1_binary/published_bank1/
  access_skill_bank_v1.json        42 Skills
  construction_skill_bank_v1.json  3 Skills
```

42 个 Access Skills 的实际形态：

| content 形态 | 数量 |
|---|---:|
| 同时含 A1 与 A2 指令 | 35 |
| 仅 A1 | 1 |
| 仅 A2 | 6 |
| 无 A1/A2 前缀 | 0 |

这不是合理的内部分类，而是 fixed-topology prompt 对候选生成和 CRUD 的
反向污染。绝大多数 Skill 实际是一条跨越检索、核验和组合的统一策略。

### 2.1 当前回答资产的有效性

完整 baseline 导出存在：

```text
outputs/flash_report_val_base/qa_results.jsonl   392 unique QA
outputs/flash_report_test_base/qa_results.jsonl  435 unique QA
```

以下 Bank1 文件题数完整，但不能作为一次干净的同步重跑：

| 文件 | 总行数 | 本轮真实 runtime 调用 | 历史恢复行 |
|---|---:|---:|---:|
| `flash_cmp_20260813_val_bank1_conv-26` | 199 | 55 | 144 |
| `flash_cmp_20260813_val_bank1_conv-41` | 193 | 131 | 62 |
| `flash_cmp_20260813_test_bank1_conv-48` | 239 | 194 | 45 |
| `flash_cmp_20260813_test_bank1_conv-49` | 196 | 95 | 101 |
| 合计 | 827 | 475 | 352 |

原因是旧 `--resume` 将 SQLite 中所有历史 `access_runs` 当成本轮 checkpoint。
不要 judge 或报告这四个文件为新 Bank1 结果。

当前没有正在运行的正式实验进程。

## 3. 当前代码架构

### 3.1 数据平面：MiMRuntime

入口：`src/mim/workflows/use.py::MiMRuntime`

核心接口：

```python
runtime = MiMRuntime(
    config,
    mode="base" | "mim",
    skill_bank=bank,
    run_dir=run_dir,
    runtime_model=model,
    embedder=embedder,
    phase="train" | "validation" | "test" | "eval_answer_only",
    persist_access=True | False,
)

runtime.ingest(conversation)      # 顺序构建整段 conversation
runtime.attach(conversation_id)   # 挂接已有 SQLite，不重新构建
result = runtime.ask(question)    # 在最新 commit snapshot 上回答
```

`attach()` 是快速 ACCESS-only 评测必须使用的接口。它只打开已经构建好的
`state/memory.sqlite3`，不执行 construction。

### 3.2 Construction

当前 construction 是固定两阶段：

```text
Session
  -> Construction Skill retrieval
  -> C1: 提取原子、可持久事实候选
  -> C2: 根据当前 snapshot 作 ADD/UPDATE/DELETE/NOOP 等决策
  -> SQLite transaction / commit
  -> versioned final memory snapshot
```

主要实现：

- `src/mim/agents/construction.py`
- `src/mim/storage/sqlite_store.py`
- `prompts/construction_extraction.md`
- `prompts/construction_decision.md`

单个 conversation 的 session/message 构建顺序必须严格串行；不同
conversation 可以并行并使用不同 API key。一个 conversation 的最终回答只
能读取其最后 commit。

### 3.3 当前 Access（需要重构）

实现：`src/mim/agents/access_v2.py::StableAccessAgent`

当前 fixed topology：

```text
question
  -> deterministic initial hybrid search
  -> 根据 question + first-search observation 召回 Top-k Access Skills
  -> LLM A1: supplemental retrieval plan + A1 applied_skill_ids
  -> 最多一轮 supplemental retrieval
  -> LLM A2: evidence selection + answer + A2 applied_skill_ids
  -> used_skill_ids = union(A1 IDs, A2 IDs)
```

当前提示词：

- `prompts/access_plan.md`
- `prompts/access_answer.md`

当前 retrieval 是 semantic + BM25 + keyword + structured 的加权 RRF：

- 实现：`src/mim/retrieval/hybrid.py::HybridRetriever`
- 配置：`config.retrieval`
- 默认初始检索使用原问题、抽取关键词和大写实体；
- A1 可以提供 supplemental query、实体、关键词、历史和时间过滤；
- A2 只能从 visible memories 选择 evidence version ID。

### 3.4 Skill Bank

运行时接口：`src/mim/skills.py::SkillBank`

```python
bank = SkillBank.load_published(bank_dir)
bank.freeze()

selected, trace = bank.retrieve_with_trace(
    query=query,
    side=Side.ACCESS | Side.CONSTRUCTION,
    embedding_index=embedder,
    top_k=...,
    candidate_k=...,
    disclose_k=...,
    min_score=...,
    reranker=None,
)
```

重要约束：

- runtime 只读已发布的物理隔离 Bank 文件；
- `description` 是主要检索表示；`content` 不参与触发匹配；
- 当前路由为 0.70 semantic + 0.30 BM25；
- fixed topology 不使用独立 LLM reranker；
- trace 同时保存 selected 和 nearby-not-selected；
- 正式 Bank 与候选/working repository 必须隔离。

维护侧：

- `src/mim/skill_maker/repository.py`：版本库；
- `src/mim/skill_maker/models.py`：candidate/operation schema；
- `src/mim/skill_maker/batch.py`：聚类、相关 Bank 召回和 CRUD 执行；
- `scripts/run_skill_bank_pipeline_v2.py`：candidate -> cluster -> draft -> CRUD
  -> transactional release。

### 3.5 控制平面：Judge、Diagnosis、Skill learning

目标流程：

```text
train predictions
  -> strict binary Judge (C/W)
  -> 只诊断 W
  -> Answer sufficiency / Access / Construction evidence analysis
  -> reusable candidate or record-only
  -> side-local clustering and batch CRUD
  -> publish immutable Bank N
  -> fresh val/test comparison
```

Judge：

- `scripts/judge_binary.py`
- `prompts/judge/locomo_binary_judge.md`
- 每个 QA 单独请求；label 只能是 `C` 或 `W`；
- 并发来自 worker，不将无关 QA 塞入同一上下文；
- thinking disabled、temperature 0 只能降低采样波动，不能保证供应商服务在
  不同时间、不同 endpoint 或模型版本下逐字/逐判一致。

Diagnosis V3 应使用明确的分步 runner。`scripts/run_diagnosis_only.py` 已标记
为 legacy，不能作为当前标准入口。诊断只能读取该阶段有权读取的证据，不能
让 access diagnosis 偷看原始对话来弥补 construction 错误。

Candidate/CRUD 提示词：

- `prompts/skill_maker/candidate_generation_access.md`
- `prompts/skill_maker/candidate_generation_construction.md`
- `prompts/skill_maker/cluster_summarizer_access.md`
- `prompts/skill_maker/cluster_summarizer_construction.md`
- `prompts/skill_maker/batch_crud_access.md`
- `prompts/skill_maker/batch_crud_construction.md`

## 4. 当前配置与模型接口

主要配置 schema：`src/mim/config.py::MiMConfig`

```yaml
models:
  runtime:      # construction + access 正常运行
  maintenance:  # judge / diagnosis / candidate / CRUD
embedding:
construction:
retrieval:
access:
training:
prompts:
```

当前快速实验配置：`configs/deepseek_v4_flash_fixed_topology.yaml`

- Runtime：`deepseek-v4-flash`
- Maintenance：`deepseek-v4-flash`
- thinking：disabled
- temperature：0.0
- Embedding：`Qwen/Qwen3-Embedding-0.6B`
- Embedding device：CPU

模型通过 `mim.llm.create_client(ModelConfig)` 创建，使用 OpenAI-compatible
接口。API key 必须来自环境变量；不得写入文档、manifest、日志或 Git。

## 5. 已确认的问题

### P0：统一 Access Skill 被 fixed A1/A2 人为切割

这是当前最重要的概念问题。

- 正式 Skill 没有阶段字段；
- 35/42 个现有 Skill 同时包含 A1 与 A2 动作；
- A1/A2 是两个独立 prompt，不是同一 message history；
- 两侧分别自报 `applied_skill_ids` 后求并集，不等价于一条 Skill 是否改变了
  完整访问—回答轨迹；
- candidate/CRUD prompt 已被这种 topology 污染，开始主动生成 `A1:`、`A2:`
  文本。

不要继续把 Bank 拆成“检索 Skill”和“回答 Skill”。正确方向见第 7 节。

### P0：旧 answer-only resume 污染本轮实验

旧 `scripts/run_existing_memory_answers.py --resume` 同时读取：

1. 本轮 partial JSONL；
2. SQLite 中所有历史 completed access runs。

这会生成题数正确、QA 唯一，却混有不同时刻/不同实验回答的文件，因此仅按
行数校验无法发现。

该问题已经在工作区修复，但尚未 commit/push，见第 6 节。

### P1：CPU embedding 并发方式错误

同一进程用 `qa-workers=4` 并发调用一个 CPU SentenceTransformer/Qwen
模型会发生严重线程争抢。此前四个 conversation × 四个 QA worker 持续数
小时仍不能完成最小 conversation。

正确并发单位应是完整 QA 或完整 conversation；本地 embedding 必须串行或
使用经过基准验证的独立服务/批处理。不能把“API key 多”直接等同于“CPU
embedding 也应并发”。

### P1：重复检索计算

此前每题重复编码 42 条固定 Skill description；同一最终 snapshot 又在
semantic/BM25/keyword/structured 路径中反复读取和解码。

工作区已加入：

- Skill document embedding cache；
- snapshot 和 stored embedding cache；
- `MemoryHit` deep-copy，避免缓存对象的 score/matched_paths 被并发污染。

### P1：实验不可观测、不可恢复

旧 answer-only runner 只在整个 conversation 完成后写 JSONL。运行数小时仍
无法知道完成了多少题，中断后结果丢失。

工作区版本已改为每题 checkpoint + flush + fsync + progress，并在完成前做
全量唯一性校验。

### P1：Answer Failure 与 Access composition 边界不一致

旧文档一处规定“已见充分证据但回答错误属于 Answer Failure，只记录，不学
memory Skill”；当前 prompt 又允许从 A2 evidence-composition failure 生成
Access Skill。需要统一：

- 若失败能抽象成可复用的 memory evidence access/use 程序（主体核验、列表
  覆盖、多跳组合、时间冲突等），可以生成统一 Access Skill；
- 若只是语言模型常识、格式、指令遵循或不可复用的答案错误，则 record-only
  或更换模型；
- failure locus 可以保留在诊断包，正式 Access Skill 不保留阶段类型。

### P2：文档与代码漂移

旧 `docs/CURRENT_HANDOVER.md` 已在清理旧文档时删除；README 仍把 fixed
A1/A2 描述为当前目标。本文档是新的真实状态，架构重构完成后必须同步更新
README 和 prompt 文档。

## 6. 当前未提交的工作区修改

截至本文档生成时，以下修改尚未 commit/push：

```text
M  scripts/run_existing_memory_answers.py
M  src/mim/retrieval/hybrid.py
M  src/mim/skills.py
M  tests/test_memory_retrieval.py
?? tests/test_existing_memory_answers.py
?? docs/CURRENT_HANDOVER.md
```

修改内容：

1. answer-only runner 的 fresh/resume/overwrite 协议重写；
2. 每轮唯一 `evaluation_run_id`；
3. 每条答案标记 `answer_source=fresh_runtime_call`；
4. resume 只读取本轮 checkpoint，不读历史 SQLite access runs；
5. 历史导出变为显式 `--export-historical-access` 并写独立文件；
6. CPU embedding 默认只允许一个 QA worker；
7. Skill description embedding cache；
8. final snapshot / stored embedding cache；
9. 对重复 QA、跨 conversation、额外 QA、缓存污染的回归测试。

当前测试：

```text
python -m pytest -q
109 passed
```

这些修改只修复实验完整性和性能，没有改动 `access_v2.py` 的 A1/A2 语义。

## 7. 推荐的目标 Access 架构

不要恢复旧版最多六步、不可控的开放 ReAct；也不要继续维护两个独立 A1/A2
prompt。建议实现一个 bounded unified access trajectory：

```text
question
  -> mandatory deterministic initial search
  -> retrieve unified Access Skills once from question + compact observation
  -> create one continuous model message history containing:
       question + initial memories + the complete selected Skills
  -> model turn 1:
       a) answer now; or
       b) request exactly one supplemental search/inspection
  -> if b:
       execute tool
       append the assistant action and full tool observation to same history
       model turn 2 must answer
  -> final response reports applied_skill_ids once for the whole trajectory
```

关键性质：

- 一个 Skill 的全部 content 在整条轨迹中持续可见；
- 不再单独计算 A1/A2 Skill usage；
- 如果初始证据充分，每题只需一次 LLM；
- 只有确实需要补检索时才需第二次；
- baseline 使用同一动作预算，但没有 Skill；
- Bank 与 baseline 的唯一区别是是否召回并注入 Skill；
- final trace 同时保存 selected skills 与全轨迹 applied skills。

建议最终 answer action：

```json
{
  "action": "answer",
  "arguments": {
    "answer": "direct grounded answer",
    "evidence_version_ids": ["visible version ID"]
  },
  "applied_skill_ids": ["sk_xxx_v1"],
  "skill_application": [
    {
      "skill_id": "sk_xxx_v1",
      "effect": "How it materially changed retrieval, evidence use, or composition."
    }
  ]
}
```

`skill_application` 是运行 trace，不进入正式 Skill schema。

## 8. 重构实施顺序

### Phase 0：保护当前工作

1. review 第 6 节未提交修改；
2. 运行全套测试；
3. commit/push 性能与 answer-only 完整性修复；
4. 不把当前混合的 Bank1 QA 文件当正式结果提交。

### Phase 1：统一 Access Runtime

1. 从 `src/mim/agents/access.py` 复用“同一 message history 保留 action 和
   observation”的成熟代码；
2. 新建或重写 bounded agent，最多一个 supplemental tool action；
3. 第一次可直接 answer，第二次必须 answer；
4. 召回的完整 Skill 只注入一次；
5. 只在最终 answer 做统一 Skill attribution；
6. 保留 selected/nearby Skill retrieval trace；
7. 修改 `AccessResult.steps` 为真实 LLM turn 数或明确的 action 数，不能混用。

### Phase 2：重写 Access prompts 与迁移 Bank

1. 用一个统一 `access_unified.md` 替代 `access_plan.md` 和
   `access_answer.md`；
2. candidate/cluster/CRUD prompt 删除所有 A1/A2 topology 要求；
3. 将 content 写成阶段中立、按执行顺序排列的动作；
4. 对当前 42 条 Skill 做机械前缀迁移（去掉 `A1:`/`A2:`），但不拆 Skill；
5. 迁移后重新做 validator、重复/冲突检查和一次正式发布。

### Phase 3：统一诊断边界

1. diagnosis 包可以记录 `retrieval`、`evidence_use`、`composition`、`mixed`
   failure locus；
2. candidate 输入应看到完整访问轨迹，不按 A1/A2 分包；
3. candidate 输出仍只能是一条统一三字段 Access Skill；
4. 无可复用 memory procedure 的 answer failure 必须 record-only。

### Phase 4：测试

必须新增：

- Skill 的多条 content 在整个轨迹持续可见；
- 第一次直接 answer 只调用一次 LLM；
- supplemental search 后第二次调用看到完整 action/observation history；
- 最终只输出一次 applied Skill attribution；
- baseline 与 Bank 的 tool/action budget 完全一致；
- Bank1 unified Skill 能同时改变搜索和答案组合；
- CPU embedding 并发锁/队列不会污染结果；
- resume 后所有行拥有同一个 `evaluation_run_id`。

### Phase 5：干净实验

先只使用已经建好的最终 memory snapshots，不重建 construction：

1. 为 baseline 和 Bank 创建全新输出目录；
2. 同一 conversation 的 baseline/Bank 使用相同 snapshot；
3. 同一轮、同一模型配置、同一 topology、同一 judge prompt；
4. 一个 QA 是不可拆分的调度单元；其模型 turn 必须串行且共享历史；
5. QA 之间可以使用 API key pool 并发，但 CPU embedding 必须串行或服务化；
6. 每题 checkpoint；最终校验 392/392 与 435/435；
7. strict binary judge 后报告 C/W、W2C、C2W 和 Skill selected/applied/impact；
8. 先看 validation，再冻结逻辑并只评一次 test；不得根据 test 改 Bank。

## 9. 禁止再次犯的错误

- 不要把 133/53/36/91 等旧子集 summary 当作完整 split；正确题数是
  199/193/239/196；
- 不要只看总行数判断实验是否干净；必须核对 `evaluation_run_id` 和
  `answer_source`；
- 不要让 `--resume` 从 memory SQLite 读取历史 access runs；
- 不要在同一 CPU embedding 实例上盲目开多个 QA worker；
- 不要因为 API key 多就并发单个 conversation 的 construction；
- 不要把统一 Access Skill 拆成检索 Bank 和回答 Bank；
- 不要让 candidate/CRUD prompt 继续产生 A1/A2 前缀；
- 不要在未完成 strict judge 前生成正式 diagnosis；
- 不要用 test 生成、筛选或修改 Skill；
- 不要把 temperature=0 当作跨 endpoint、跨时间绝对确定性的保证；
- 不要将任何 API key 写入脚本、文档、manifest、日志或 Git。

## 10. 常用命令

测试：

```powershell
cd D:\Documents\Project\Memory_in_Memory\single_agent_mim
$env:PYTHONPATH = "src"
python -m pytest -q
python main.py smoke
```

对已建 memory 做干净 answer-only（当前 runner，仍使用旧 A1/A2 runtime，
重构前只用于验证 runner）：

```powershell
python scripts/run_existing_memory_answers.py `
  --config configs/deepseek_v4_flash_fixed_topology.yaml `
  --run-id <fresh-run-id> `
  --output-dir outputs `
  --conversation-id conv-26 `
  --skill-bank-dir outputs/fullstack_v4p_20260813_bank1_binary/published_bank1 `
  --qa-workers 1
```

恢复只能使用同一 run 的 checkpoint：

```powershell
python scripts/run_existing_memory_answers.py <same arguments> --resume
```

显式重跑并覆盖 QA 结果文件（不会删除 memory SQLite）：

```powershell
python scripts/run_existing_memory_answers.py <same arguments> --overwrite
```

严格 Judge：

```powershell
python scripts/judge_binary.py `
  --config configs/deepseek_v4_flash_fixed_topology.yaml `
  --judge-model deepseek-v4-flash `
  --workers <N> `
  --output-dir <fresh-judge-dir> `
  <prediction-jsonl-files>
```

## 11. 外部设计依据

- ReAct：reasoning、action、observation 应在同一轨迹中交错并保持历史，支持
  根据新观察调整计划：<https://arxiv.org/abs/2210.03629>
- ExpeL：召回统一的自然语言 insights 指导完整 agent execution，而不是将
  insight 固定分配给一个 prompt 阶段：<https://arxiv.org/abs/2308.10144>
- LangMem：procedural memory 是指导 agent 如何行动和回答的整体系统指令：
  <https://langchain-ai.github.io/langmem/concepts/conceptual_guide/>
- Mem0：插件边界主要是 memory add/search，回答器消费检索结果；MiM 应能
  包装这一接口，而不是替换其存储：<https://github.com/mem0ai/mem0>
- SimpleMem：通过 intent-aware planning 决定多视图检索范围，说明查询规划
  可以保留，但不要求把程序性经验拆成多个 Bank 类型：
  <https://github.com/aiming-lab/SimpleMem/blob/main/docs/text-memory.md>

## 12. 接手者的第一项任务

不要立即重跑 827 题。先完成第 7 节的 unified bounded Access 设计评审，
然后按 Phase 0–4 实现并用少量 train QA 做协议/trace/调用次数验证。确认：

1. 一条 Skill 在整条问题轨迹中完整可见；
2. 最终只归因一次；
3. 初始证据充分时只有一次 LLM；
4. 需要补检索时最多两次 LLM且共享消息历史；
5. baseline/Bank 拥有完全相同的动作预算。

满足这些条件后，才启动新的 baseline + Bank1 同步全量评测。

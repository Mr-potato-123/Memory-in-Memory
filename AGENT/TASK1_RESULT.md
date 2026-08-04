# TASK1：MiM 最小可用评测版代码交接说明

> 目标：实现一个不臃肿、可以跑通 LoCoMo 训练与评测、能够清楚验证 MiM 核心机制的 Single-Agent Demo。  
> 原则：减少基础设施和过度抽象，但不省略四个 Agent、完整 Workflow、模型接口、版本记录和可复现实验接口。
>
> Construction / Access 的实现级存储、动作、更新、混合检索、时间版本和 Replay 设计见 `AGENT/TASK1_RUNTIME_ARCHITECTURE.md`。该文件的运行侧设计优先于本文的概览；其中 SQLite 取代早期 JSON 主存储建议，JSON 仅保留为导出和实验 artifact。
>
> 独立维护侧 Failure 子系统见 `AGENT/TASK1_FAILURE_ARCHITECTURE.md`。它只读消费标准化 FailureInputBundle，不属于 Runtime Workflow，也不向 Runtime Memory Store 写入状态。

---

## 1. 一句话方案

实现一个 Python 单进程评测工程：

- 运行侧使用弱模型；
- 维护侧使用强模型；
- 四个 Agent 各自有独立类、独立 prompt 和明确输入输出；
- 三个 Workflow 分别负责日常使用、Skill 训练和冻结评测；
- Memory 与 Skill 使用 JSON 文件保存版本；
- GPT/Qwen 通过 OpenAI-compatible 接口接入，Claude 通过 Anthropic 接口接入；
- 对外只提供 `use`、`train`、`evaluate` 三个主要入口。

```text
运行侧（弱模型）
├─ Construction Agent
└─ Access & Answer Agent

维护侧（强模型）
├─ Failure Agent
└─ Skill-Maker
```

这里的四个 Agent 是四种清晰的逻辑职责，不需要部署成四个服务。

---

## 2. 什么叫“最小可用”

### 2.1 必须保留

以下功能不能因为“最小化”而删除：

1. 四个 Agent 的职责与代码边界；
2. Construction 和 Access 两个运行阶段；
3. Failure Attribution；
4. Construction/Access 双 Skill Bank；
5. Candidate Skill 的强制验证和自然验证；
6. Memory 与 Skill 的基础版本记录；
7. train/validation/test 的 conversation-level 隔离；
8. Base 和 MiM 的公平对比；
9. 使用、训练、评测三个对外接口；
10. 强弱模型分离与可替换 Provider；
11. 关键 trace 和实验结果；
12. 最基本的单元测试和 smoke test。

### 2.2 本版不做

以下属于生产化或过度扩展，当前不实现：

- Web UI；
- HTTP/MCP 服务；
- Docker；
- 数据库和 migration；
- Redis、消息队列、异步 worker；
- 多进程 Agent；
- 用户、权限和租户系统；
- LangGraph 工作流；
- 通用插件市场；
- 多 benchmark 框架；
- 图数据库；
- 自动扩缩容；
- 完整成本计费；
- Skill 自动退休、复杂聚类和长期治理；
- LangMem/MIRIX/Mem0/A-MEM 的正式 adapter。

### 2.3 为什么仍然需要版本和 Replay

这三项看似增加代码，但属于研究验证的最低要求：

- **Memory session 快照**：否则无法判断事实在哪个构建阶段丢失；
- **Skill Bank 版本**：否则无法复现实验，也无法回退错误 Skill；
- **Replay**：否则只能证明“写了一条 Skill”，不能证明 Skill 修复了错误。

它们采用普通 JSON 文件实现，不引入数据库。

---

## 3. 项目背景

Memory in Memory（MiM）不是新的记忆数据库，而是添加在 Agent Memory System 外部的错误驱动元记忆层。

基础记忆系统有两个核心阶段：

```text
Memory Construction
  从历史对话中选择、抽取和更新长期 Memory。

Memory Access
  根据当前问题检索和组织 Memory，并给出答案。
```

两侧会重复出现可迁移的失败模式：

### Construction 常见错误

- 该写入的事实没有写；
- 新状态直接覆盖旧状态；
- 时间信息丢失；
- 多个事实错误合并；
- 无关信息被写入；
- 更新时丢失来源。

### Access 常见错误

- 查询表达不充分；
- 没有使用时间检索；
- 只返回语义相似但时间错误的事实；
- 没有组合多条 Memory；
- 正确信息已经存在但没有召回；
- 检索已经充分却继续搜索或过早停止。

MiM 从这些错误中学习的是自然语言 Skill：

> 遇到类似记忆场景时，应该如何构建或访问记忆。

Skill 不是脚本，不包含具体用户答案，也不需要 PPO、微调或参数训练。

---

## 4. 本 Demo 要验证的问题

本 Demo 只需要回答四个研究问题：

1. 弱运行模型能否在 Skill 指导下减少记忆错误；
2. 强维护模型能否正确区分 Construction、Access 和非记忆错误；
3. 从 train conversation 学到的 Skill 能否迁移到未见 conversation；
4. MiM 的提升是否来自 Skill，而不是更强模型、更多工具或更多运行轮数。

为此，最小主实验为：

```text
Single-Agent Base
Single-Agent + MiM-Construction
Single-Agent + MiM-Access
Single-Agent + MiM-Joint
```

时间允许再增加：

```text
Single-Agent + Global Reflection
Single-Agent + Retrieved Failure Cases
```

---

## 5. 四个 Agent

### 5.1 Construction Agent

### 角色

将按时间到来的 session 对话转换成长期 Memory。

### 模型

使用 `runtime` 弱模型。

### 输入

- 当前 session messages；
- 当前 Memory；
- 当前 session 时间；
- 检索到的 Construction Skills；
- 动作预算。

Base 模式下 Skills 为空。

### 动作

评测版只保留四个动作：

```text
search_memory
add_memory
update_memory
finish
```

不实现 delete。需要删除或纠正时使用 `update_memory` 产生新版本，并保留旧版本。

### 输出

- 更新后的 Memory；
- action trace；
- session Memory snapshot；
- 使用的 Skill IDs；
- token、延迟和错误信息。

### 核心要求

- Agent 自主决定写什么；
- 写入必须包含 source message ID；
- 状态变化必须尽量保留时间；
- update 不覆盖旧版本；
- Base 与 MiM 使用相同动作和最大步数。

---

### 5.2 Access & Answer Agent

### 角色

围绕当前问题检索 Memory，并在同一个 Agent loop 中直接输出最终答案。

评测版不单独创建 Answer Agent。Access 与 Answer 是同一个 Agent 的两个连续行为：每轮先查看当前证据，再自主选择继续检索，或者执行 `answer` 动作结束。

```text
question
→ 判断当前证据是否充分
→ search_memory
→ 查看新证据
→ 继续 search_memory 或 answer
→ 最终答案
```

因此它可以“边 Access、边回答”：模型始终同时看到问题、已经检索到的证据和剩余动作预算，不需要等独立检索模块一次性返回固定 Top-k 后，再交给另一个 Answer Agent。

### 模型

使用与 Construction 相同的 `runtime` 弱模型。

### 输入

- question；
- 当前 conversation 的 final Memory；
- 检索到的 Access Skills；
- 搜索预算。

### 动作

```text
search_memory
answer
```

`search_memory` 通过参数选择检索方式：

```text
semantic
keyword
temporal
```

这样既保留 Access & Answer Agent 的工具选择能力，又不需要维护三个几乎重复的工具实现。

### 输出

- answer；
- evidence Memory version IDs；
- search trace；
- 使用的 Skill IDs；
- token、延迟和错误信息。

### 核心要求

- 只能根据检索到的 Memory 回答；
- evidence ID 必须来自本轮检索结果；
- Agent 自主决定查询、检索方法、是否继续和何时回答；
- 每次搜索结果都会立即返回同一个 Agent 上下文；
- 不额外调用独立 Answer 模型；
- Base 与 MiM 使用相同检索方法、top-k 和最大步数。

---

### 5.3 Failure Agent

### 角色

当 train QA 回答错误时，判断错误发生在哪一侧。

### 模型

使用 `maintenance` 强模型。

### 输入

- question；
- prediction；
- reference answer；
- LoCoMo source evidence 原文；
- final Memory；
- 与 source/reference 相关的 Memory；
- Access search trace；
- 最终 evidence；
- 当次使用的 Skill IDs。

### 输出类别

```text
construction
  原始对话存在正确事实，但 Memory 没有正确保存。

access
  Memory 已存在正确事实，但 Access 没有形成正确证据。

other
  正确证据已经提供，仍因回答或推理出错。

invalid
  原始对话不支持答案、标注冲突或证据不足。
```

### 输出格式

```json
{
  "label": "access",
  "confidence": 0.92,
  "reason": "The correct historical state exists in memory but was not retrieved.",
  "source_evidence_ids": ["conv2_s4_m7"],
  "memory_evidence_ids": ["mem_0012_v1"],
  "access_evidence_ids": ["mem_0012_v2"],
  "failure_signature": "wrong_temporal_version"
}
```

### 核心要求

- `other` 和 `invalid` 只记录，不更新 Skill；
- Failure Agent 不能修改事实 Memory；
- 完整 Memory 保存在 artifact 中，但 prompt 只注入相关片段，避免上下文过长。

虽然 Access 与 Answer 由同一个 Agent 完成，归因时仍可依靠 trace 区分：正确 Memory 从未进入该 Agent 已见证据时属于 `access`；正确证据已经进入上下文但最终答案仍错时属于 `other`。因此合并执行者不会破坏 MiM 的错误归因。

---

### 5.4 Skill-Maker

> Skill-Maker 的最新实现级设计以 `AGENT/TASK1_SKILL_MAKER_ARCHITECTURE.md` 为准；本节只保留总体说明。

### 角色

把 Construction/Access failure 抽象为可复用 Skill，并管理 Candidate。

### 模型

使用 `maintenance` 强模型。

### 输入

- Failure Agent 产生的 failure trajectory；
- 当前失败案例；
- Draft 阶段之后才提供相似 Skill；
- Replay 结果。

### 工作步骤

```text
1. 独立生成 Draft
2. 检索同侧相似 Skill
3. 决定 create / revise / merge / reuse
4. 生成 Candidate
5. forced replay
6. natural replay
7. 通过则写新 Bank 版本，否则修订或拒绝
```

### Skill 最小正文

```json
{
  "name": "Select memory by target time",
  "description": "Use when an entity has multiple states over time.",
  "content": "Identify the target time first. Retrieve and use the state valid at that time rather than the most semantically similar state."
}
```

`skill_id/version/side/status/parent/source failure` 属于系统元数据，不进入模型可见的 Skill 正文。

### 核心要求

- Draft 生成时不能先看旧 Skill；
- Construction 与 Access Skill 分开；
- Skill 不包含具体人物、地点和参考答案；
- Candidate 通过验证前不能进入 Active Bank；
- Skill 更新保存新版本，不覆盖旧文件。

---

## 6. 三个核心 Workflow

### 6.1 使用 Workflow

用于人工 Demo 或单个 conversation 问答。

```text
conversation sessions
→ Construction Skill Retrieval（Base 跳过）
→ Construction Agent
→ 每个 session 保存 Memory snapshot
→ question
→ Access Skill Retrieval（Base 跳过）
→ Access & Answer Agent
→ answer + evidence + trace
```

对外接口：

```python
runtime = MiMRuntime(config, mode="mim", skill_bank=bank)
runtime.ingest(conversation)
result = runtime.ask(question)
```

CLI：

```powershell
python main.py use `
  --conversation data\demo.json `
  --question "Where does Alice currently live?" `
  --mode mim `
  --skill-bank outputs\train_001\skills\selected.json
```

返回：

```json
{
  "answer": "Seattle",
  "evidence_ids": ["mem_0007_v2"],
  "construction_skill_ids": [],
  "access_skill_ids": ["access_temporal_001_v2"],
  "trace_path": "outputs/use_001/traces.jsonl"
}
```

---

### 6.2 训练 Workflow

这里的“训练”是 Skill Bank 的非参数化学习，不训练 LLM 权重。

```text
读取 train conversations
→ 当前 Skill Bank 下构建 Memory
→ 回答 QA
→ 判断 QA 是否失败
→ Failure Agent
→ construction/access 才进入 Skill-Maker
→ Candidate
→ forced replay
→ natural replay
→ 通过后保存新 Skill Bank version
→ 继续下一个案例
→ 在 validation conversations 上比较各 Bank version
→ 生成只读 selected Skill Bank
```

对外接口：

```python
trainer = MiMTrainer(config)
train_result = trainer.train(
    dataset=dataset,
    conversation_ids=split.train,
    validation_ids=split.validation,
    initial_skill_bank="skills/skill_bank_v000.json",
)
```

CLI：

```powershell
python main.py train `
  --config configs\default.yaml `
  --dataset ..\LoCoMo\data\locomo10.json `
  --split data\splits\locomo_6_2_2.json `
  --run-id train_001
```

训练输出：

- 每个 conversation 的 Memory；
- QA results；
- Failure Attribution；
- Draft/Candidate；
- Replay；
- Skill Bank 各版本；
- validation 上的 Bank version 对比；
- 最终只读 `selected.json`；
- training summary。

训练约束：

- 只允许 train IDs 生成或修改 Skill；
- validation IDs 仅用于比较 Bank version，不触发维护侧；
- conversation 顺序固定；
- Skill Bank 串行更新；
- 不使用 validation/test 生成 Skill；
- 每次运行保存 resolved config 和 dataset hash。

---

### 6.3 评测 Workflow

评测使用冻结模型、prompt、预算和 Skill Bank。

```text
加载 split
→ 加载冻结配置
→ Base：空 Skill Bank
   或
→ MiM：selected Skill Bank
→ 对每个 conversation 重新构建独立 Memory
→ 回答所有 QA
→ 计算 LoCoMo-compatible 指标
→ 输出 summary
```

对外接口：

```python
evaluator = MiMEvaluator(config)
report = evaluator.evaluate(
    dataset=dataset,
    conversation_ids=split.test,
    mode="mim",
    skill_bank="outputs/train_001/skills/selected.json",
)
```

CLI：

```powershell
# Base
python main.py evaluate `
  --config configs\default.yaml `
  --split-name test `
  --mode base `
  --run-id test_base_001

# MiM
python main.py evaluate `
  --config configs\default.yaml `
  --split-name test `
  --mode mim `
  --skill-bank outputs\train_001\skills\selected.json `
  --run-id test_mim_001
```

评测约束：

- test 不运行 Failure Agent；
- test 不运行 Skill-Maker；
- test 不产生新 Skill；
- Base 和 MiM 的 runtime 模型及预算完全一致；
- 每个 conversation 使用独立 Memory；
- 不根据 test 结果修改配置。

---

## 7. Agent Workflow 总图

```text
                         ┌────────────────────────────┐
                         │     Skill Bank（可为空）    │
                         │ Construction / Access 分侧 │
                         └───────────┬────────────────┘
                                     │ retrieve
                                     ▼
Raw Sessions ──→ Construction Agent（弱模型）
                         │
                         ▼
                Versioned Memory
                         │
Question ────────────────┤
                         ▼
              Access & Answer Agent（弱模型）
                         │
                         ▼
                Answer + Evidence
                         │
                         ▼
                    QA Metric
                         │
          ┌──────────────┴──────────────┐
          │ correct                     │ wrong + train
          ▼                             ▼
       保存结果                 Failure Agent（强模型）
                                        │
                         ┌──────────────┼──────────────┐
                         │              │              │
                  construction       access      other/invalid
                         │              │              │
                         └──────┬───────┘              └→ 只记录
                                ▼
                       Skill-Maker（强模型）
                                │
                                ▼
                         Candidate Skill
                                │
                       forced + natural replay
                                │
                         accept / reject
                                │
                                ▼
                        New Skill Bank Version
```

---

## 8. 模型接口

### 8.1 强弱模型配置

不在代码中写死模型名称：

```yaml
models:
  runtime:
    provider: openai_compatible
    model: ${RUNTIME_MODEL}
    api_key_env: RUNTIME_API_KEY
    base_url: ${RUNTIME_BASE_URL}
    temperature: 0.0
    max_tokens: 1200
    timeout_seconds: 90
    max_retries: 3
    supports_json_mode: true

  maintenance:
    provider: anthropic
    model: ${MAINTENANCE_MODEL}
    api_key_env: MAINTENANCE_API_KEY
    base_url: null
    temperature: 0.0
    max_tokens: 2500
    timeout_seconds: 180
    max_retries: 3
    supports_json_mode: false
```

可配置为：

```text
Qwen 弱模型 + Claude 强模型
Qwen 弱模型 + GPT 强模型
GPT 弱模型 + Claude 强模型
GPT 弱模型 + GPT 强模型
```

### 8.2 统一 ModelClient

```python
class ModelClient(Protocol):
    def generate(
        self,
        messages: list[dict],
        *,
        temperature: float,
        max_tokens: int,
        json_mode: bool = False,
    ) -> ModelResponse:
        ...
```

统一返回：

```python
class ModelResponse(BaseModel):
    text: str
    provider: str
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    latency_ms: int
    finish_reason: str | None
```

### 8.3 Provider

### OpenAICompatibleClient

支持：

- GPT 官方 API；
- Qwen OpenAI-compatible API；
- 其他兼容 endpoint。

配置不同 `base_url` 即可，上层 Agent 不需要知道模型厂商。

### AnthropicClient

支持 Claude：

- adapter 负责 system message 转换；
- adapter 负责 usage 格式转换；
- 上层仍使用统一 JSON action 协议。

### MockClient

用于：

- 单元测试；
- 无 API key smoke test；
- 模拟非法 JSON；
- 固定 replay。

### 8.4 为什么不用原生 Tool Calling

GPT、Qwen、Claude 的 tool calling 格式和兼容性不同。为保持最小和公平，本 Demo 让模型输出统一 JSON：

```json
{
  "action": "search_memory",
  "arguments": {
    "method": "temporal",
    "query": "Alice residence before May 2023",
    "top_k": 5
  },
  "reason": "The question asks for a past state."
}
```

Python 解析后执行本地动作，再将 observation 返回给模型。

这样可以：

- 三种模型复用同一 Agent loop；
- 统一记录 action；
- 统一限制预算；
- 避免厂商 tool calling 能力成为实验变量。

---

## 9. Memory 与 Skill 存储

### 9.1 Memory Record

```json
{
  "memory_id": "mem_0007",
  "version": 2,
  "content": "Alice currently lives in Seattle and previously lived in Boston.",
  "event_time": "2023-05",
  "source_message_ids": ["conv0_s1_m2", "conv0_s3_m8"],
  "previous_version_id": "mem_0007_v1",
  "status": "active"
}
```

规则：

- add 产生 v1；
- update 产生 v2/v3；
- 旧版本进入 history；
- temporal search 同时检查 active/history；
- 不做物理删除；
- conversation 之间完全隔离。

### 9.2 Memory 快照

每个 session 后保存：

```text
memory/
└─ conv_0/
   ├─ session_01.json
   ├─ session_02.json
   ├─ session_03.json
   └─ final.json
```

快照格式：

```json
{
  "conversation_id": "conv_0",
  "after_session": "session_03",
  "active": [],
  "history": [],
  "construction_trace_id": "trace_conv0_s03"
}
```

### 9.3 Skill Bank

一个 JSON 文件同时保存两侧 Skill，通过 `side` 隔离：

```text
skills/
├─ skill_bank_v000.json
├─ skill_bank_v001.json
├─ skill_bank_v002.json
├─ candidates/
└─ selected.json
```

规则：

- v000 为空；
- accepted Candidate 生成新版本；
- rejected Candidate 只保存在 candidates；
- validation 选定一个版本为 selected；
- test 只读 selected。

### 9.4 检索

固定本地 embedding：

```yaml
embedding:
  model: sentence-transformers/all-MiniLM-L6-v2
  device: cpu
```

Memory 和 Skill 都使用：

- 小规模本地 embedding；
- NumPy cosine；
- 简单 token overlap；
- 时间字段过滤。

LoCoMo 只有 10 个 conversation，不需要向量数据库。

---

## 10. Replay

### 10.1 Forced Replay

强制把 Candidate 注入对应 Agent：

> 模型一定看到这条 Skill 时，内容是否能修复错误？

### 10.2 Natural Replay

Candidate 临时加入 Skill Bank，走正常检索：

> Skill 能否自然被召回并修复错误？

结果分为：

```text
not_retrieved
retrieved_but_failed
passed
protocol_error
```

### 10.3 Construction Replay

Construction Skill 会改变 Memory，所以需要：

```text
原始 sessions
→ 从空 Memory 重新构建
→ 注入 Candidate
→ 重新回答失败 QA
```

### 10.4 Access Replay

Access Skill 不改变 Memory：

```text
载入失败时 final Memory
→ 注入 Candidate
→ 重新检索和回答
```

### 10.5 最小回归

第一版不做复杂回归系统，只维护最多 10 条已经修复的 train failure：

- Candidate 必须继续通过这些案例；
- 只使用 train 数据；
- validation/test 不进入 replay buffer。

---

## 11. LoCoMo 训练与评测

### 11.1 6:2:2

必须按 conversation 划分：

```text
train: 6 conversations
validation: 2 conversations
test: 2 conversations
```

禁止按 QA 拆分同一 conversation。

split 文件：

```json
{
  "dataset_sha256": "...",
  "seed": 42,
  "train": ["conv_0", "conv_1", "conv_2", "conv_3", "conv_4", "conv_5"],
  "validation": ["conv_6", "conv_7"],
  "test": ["conv_8", "conv_9"]
}
```

实际 IDs 由脚本一次生成后固定提交。

### 11.2 Train

- 允许更新 Skill；
- conversation 串行；
- 当前 accepted Bank 作用于后续 train conversation；
- 保存所有 failure/candidate/replay。

### 11.3 Validation

- 不生成新 Skill；
- 从 train Bank versions 中选择版本；
- 选择 Skill top-k；
- 选择相似度阈值；
- 平分时优先 Skill 更少、成本更低的版本。

### 11.4 Test

- Skill、prompt、模型和预算全部冻结；
- 不调用维护侧；
- Base 和 MiM 各运行一次；
- 不根据 test 结果调参。

---

## 12. 最终项目树

```text
single_agent_mim/
├─ README.md
├─ requirements.txt
├─ .env.example
├─ main.py
├─ configs/
│  └─ default.yaml
├─ prompts/
│  ├─ construction.md
│  ├─ access.md
│  ├─ failure.md
│  └─ skill_maker.md
├─ src/
│  └─ mim/
│     ├─ __init__.py
│     ├─ config.py
│     ├─ schemas.py
│     ├─ artifacts.py
│     ├─ llm/
│     │  ├─ __init__.py
│     │  ├─ base.py
│     │  ├─ factory.py
│     │  ├─ openai_compatible.py
│     │  ├─ anthropic_client.py
│     │  └─ mock_client.py
│     ├─ agents/
│     │  ├─ __init__.py
│     │  ├─ construction.py
│     │  ├─ access.py
│     │  ├─ failure.py
│     │  └─ skill_maker.py
│     ├─ memory.py
│     ├─ retrieval.py
│     ├─ skills.py
│     ├─ replay.py
│     ├─ workflows/
│     │  ├─ __init__.py
│     │  ├─ use.py
│     │  ├─ train.py
│     │  └─ evaluate.py
│     └─ eval/
│        ├─ __init__.py
│        ├─ locomo.py
│        └─ metrics.py
├─ data/
│  └─ splits/
│     └─ locomo_6_2_2.json
├─ outputs/
│  └─ .gitkeep
└─ tests/
   ├─ fixtures/
   │  └─ tiny_locomo.json
   ├─ test_agents.py
   ├─ test_memory.py
   ├─ test_skills.py
   └─ test_workflows.py
```

这棵树刻意让四个 Agent 和三个 Workflow 一眼可见，同时避免数据库、服务层和多余 adapter 框架。

---

## 13. 文件职责

### 13.1 根目录

### `main.py`

唯一 CLI 入口，仅负责：

- 解析 `use/train/evaluate/smoke`；
- 加载配置；
- 初始化 Workflow；
- 打印输出位置。

业务逻辑不得写入 `main.py`。

### `README.md`

至少包含：

- 项目背景；
- 安装；
- 强弱模型配置；
- 三个核心命令；
- 输出说明；
- Base/MiM 公平性；
- 常见故障。

### `requirements.txt`

建议：

```text
openai
anthropic
pydantic
PyYAML
numpy
sentence-transformers
tqdm
pytest
```

### `.env.example`

```dotenv
RUNTIME_API_KEY=
RUNTIME_BASE_URL=
RUNTIME_MODEL=

MAINTENANCE_API_KEY=
MAINTENANCE_MODEL=
```

不得保存真实 key。

---

### 13.2 公共模块

### `config.py`

- YAML 加载；
- 环境变量替换；
- Pydantic 校验；
- 输出 resolved config；
- 计算配置 hash。

### `schemas.py`

集中保存公共数据类型：

```text
Message
Session
Conversation
Question
MemoryRecord
MemorySnapshot
SkillRecord
AgentAction
AccessResult
FailureAttribution
ReplayResult
QAResult
ModelResponse
```

避免为每个 schema 新建文件。

### `artifacts.py`

- 创建 run 目录；
- 保存 JSON/JSONL/YAML；
- 原子写文件；
- 管理 manifest；
- 防止同名 run 被覆盖。

它不包含 Agent 业务逻辑。

---

### 13.3 LLM 模块

### `llm/base.py`

定义 `ModelClient` 和通用异常。

### `llm/factory.py`

根据配置创建：

```text
openai_compatible
anthropic
mock
```

### `llm/openai_compatible.py`

负责 GPT/Qwen：

- `base_url`；
- timeout；
- retry；
- JSON mode；
- usage 转换。

### `llm/anthropic_client.py`

负责 Claude：

- system message 转换；
- response text；
- usage 转换；
- timeout/retry。

### `llm/mock_client.py`

返回 fixture 中的固定输出。

---

### 13.4 Agent 模块

四个文件各只实现一个角色：

### `agents/construction.py`

```python
class ConstructionAgent:
    def run(
        self,
        session: Session,
        memory: MemoryStore,
        skills: list[SkillRecord],
    ) -> ConstructionResult:
        ...
```

### `agents/access.py`

```python
class AccessAgent:
    def run(
        self,
        question: Question,
        memory: MemoryStore,
        skills: list[SkillRecord],
    ) -> AccessResult:
        ...
```

### `agents/failure.py`

```python
class FailureAgent:
    def attribute(
        self,
        case: FailureCase,
    ) -> FailureAttribution:
        ...
```

### `agents/skill_maker.py`

```python
class SkillMaker:
    def draft(self, failure: FailureAttribution) -> SkillDraft:
        ...

    def integrate(
        self,
        draft: SkillDraft,
        similar_skills: list[SkillRecord],
    ) -> SkillCandidate:
        ...
```

这样代码人员不会把运行侧和维护侧混在一个大文件里。

---

### 13.5 状态与 Workflow

### `memory.py`

`JsonMemoryStore`：

```text
reset
list_active
list_history
add
update
save_snapshot
load_snapshot
```

### `retrieval.py`

```text
semantic_search
keyword_search
temporal_search
```

### `skills.py`

`SkillBank`：

```text
load
retrieve
make_temporary_bank
accept_candidate
reject_candidate
select_version
freeze
```

### `replay.py`

```text
replay_construction
replay_access
forced_replay
natural_replay
regression_check
```

### `workflows/use.py`

组合 Construction + Access，不调用维护侧。

### `workflows/train.py`

组合全部四个 Agent，控制 Candidate 和 Bank 更新。

### `workflows/evaluate.py`

只组合 Construction + Access，强制 Skill Bank 只读。

### `eval/locomo.py`

- 加载 LoCoMo；
- 规范化 conversation/session/message/QA；
- 保留 category 和 source；
- 应用固定 split。

### `eval/metrics.py`

- LoCoMo-compatible normalization；
- category-aware F1；
- aggregate；
- 不调用 Agent。

---

## 14. 配置文件

```yaml
seed: 42
output_dir: outputs

dataset:
  path: ../LoCoMo/data/locomo10.json
  split: data/splits/locomo_6_2_2.json

models:
  runtime:
    provider: openai_compatible
    model: ${RUNTIME_MODEL}
    api_key_env: RUNTIME_API_KEY
    base_url: ${RUNTIME_BASE_URL}
    temperature: 0.0
    max_tokens: 1200
    max_retries: 3
    supports_json_mode: true

  maintenance:
    provider: anthropic
    model: ${MAINTENANCE_MODEL}
    api_key_env: MAINTENANCE_API_KEY
    temperature: 0.0
    max_tokens: 2500
    max_retries: 3
    supports_json_mode: false

embedding:
  model: sentence-transformers/all-MiniLM-L6-v2
  device: cpu

construction:
  max_steps_per_session: 8
  skill_top_k: 3

access:
  max_steps_per_question: 5
  memory_top_k: 5
  skill_top_k: 3

training:
  max_skill_iterations: 3
  replay_buffer_size: 10
  require_forced_replay: true
  require_natural_replay: true

prompts:
  construction: prompts/construction.md
  access: prompts/access.md
  failure: prompts/failure.md
  skill_maker: prompts/skill_maker.md
```

所有影响结果的字段都随 run 保存为 `config.resolved.yaml`。

---

## 15. JSON Action 协议

### 15.1 Construction

搜索：

```json
{
  "action": "search_memory",
  "arguments": {
    "method": "semantic",
    "query": "Alice residence",
    "top_k": 5
  },
  "reason": "Check for an existing state."
}
```

新增：

```json
{
  "action": "add_memory",
  "arguments": {
    "content": "Alice moved to Seattle in May 2023.",
    "event_time": "2023-05",
    "source_message_ids": ["conv0_s3_m8"]
  },
  "reason": "This is a durable state update."
}
```

更新：

```json
{
  "action": "update_memory",
  "arguments": {
    "memory_id": "mem_0007",
    "content": "Alice currently lives in Seattle and previously lived in Boston.",
    "event_time": "2023-05",
    "source_message_ids": ["conv0_s1_m2", "conv0_s3_m8"]
  },
  "reason": "Preserve both states."
}
```

结束：

```json
{
  "action": "finish",
  "arguments": {},
  "reason": "All durable facts have been processed."
}
```

### 15.2 Access

检索：

```json
{
  "action": "search_memory",
  "arguments": {
    "method": "temporal",
    "query": "Alice residence before May 2023",
    "top_k": 5
  },
  "reason": "The question asks for a past state."
}
```

回答：

```json
{
  "action": "answer",
  "arguments": {
    "answer": "Boston",
    "evidence_ids": ["mem_0007_v1"]
  },
  "reason": "The historical version supports the answer."
}
```

### 15.3 解析失败

流程：

```text
直接解析 JSON
→ 尝试提取代码块
→ 尝试提取首个完整 JSON
→ 同一模型执行一次格式修复
→ 仍失败则 protocol_error
```

非法 action 或参数由 Pydantic 拒绝，不能静默猜测。

---

## 16. 输出目录

```text
outputs/
└─ <run_id>/
   ├─ manifest.json
   ├─ config.resolved.yaml
   ├─ summary.json
   ├─ qa_results.jsonl
   ├─ traces.jsonl
   ├─ memory/
   │  └─ <conversation_id>/
   │     ├─ session_01.json
   │     └─ final.json
   ├─ failures/
   ├─ candidates/
   ├─ replays/
   └─ skills/
      ├─ skill_bank_v000.json
      ├─ skill_bank_v001.json
      └─ selected.json
```

### `manifest.json`

记录：

- run ID；
- phase；
- mode；
- dataset hash；
- split IDs；
- config hash；
- runtime/maintenance model；
- Skill Bank version；
- 开始和结束时间；
- 完成状态。

### `qa_results.jsonl`

```json
{
  "conversation_id": "conv_8",
  "qa_id": "qa_12",
  "category": 2,
  "question": "...",
  "reference": "...",
  "prediction": "...",
  "evidence_ids": ["mem_0007_v2"],
  "skill_ids": ["access_temporal_001_v2"],
  "f1": 1.0,
  "runtime_tokens": 843,
  "search_steps": 2
}
```

---

## 17. 指标与公平性

### 17.1 QA 指标

至少输出：

- overall F1；
- category-wise F1；
- QA 数量；
- protocol error 数量。

指标应兼容 `LoCoMo/task_eval/evaluation.py` 的归一化和 category 规则。

### 17.2 MiM 指标

- Construction/Access/Other/Invalid 数量；
- forced replay pass rate；
- natural retrieval rate；
- natural replay pass rate；
- Candidate accept/reject 数量；
- train 学到、test 命中的 Skill 数；
- 每条 Skill 修复的案例数。

### 17.3 成本指标

- runtime tokens；
- maintenance tokens；
- 平均 Construction steps；
- 平均 Access steps；
- latency；
- retry/API error 数量。

### 17.4 公平性

Base 与 MiM 固定相同：

- runtime model；
- temperature；
- Memory schema；
- embedding；
- action；
- max steps；
- memory top-k；
- answer 规则；
- dataset split。

MiM 唯一额外输入是 Skill 文本。

---

## 18. 基本可维护性要求

最小项目也必须遵守以下规则：

### 18.1 单一职责

- 四个 Agent 各一个文件；
- 三个 Workflow 各一个文件；
- Provider 与 Agent 分离；
- metrics 不调用 Agent；
- artifact writer 不包含业务逻辑。

### 18.2 类型和 Schema

- Agent 输入输出使用 Pydantic；
- 禁止跨模块传递含义不明的任意 dict；
- JSON artifact 使用 schema 中的字段名；
- schema 变化必须修改版本或 migration note，即使没有数据库。

### 18.3 Prompt 外置

- prompt 不写在 Python 大字符串中；
- prompt 文件带版本；
- run manifest 记录 prompt 文件 hash；
- 修改 prompt 后结果视为新实验配置。

### 18.4 配置可复现

- 模型名不硬编码；
- 环境变量只保存 key/endpoint；
- 所有实验参数来自 YAML；
- 每个 run 保存 resolved config；
- 禁止 Base/MiM 各自使用隐藏默认值。

### 18.5 日志可读

终端只打印：

- 当前 phase；
- conversation/QA 进度；
- 当前 Bank version；
- 错误和重试；
- 最终 summary 和输出路径。

详细 prompt、action 和 observation 写入 JSONL，不在终端刷屏。

### 18.6 依赖方向

推荐：

```text
schemas/config
      ↑
llm + memory + skills
      ↑
agents
      ↑
workflows
      ↑
main.py
```

底层模块不得反向 import workflow。

---

## 19. 测试

评测版只需要四组测试文件。

### `test_agents.py`

- Construction action 校验；
- Access evidence 校验；
- Failure 四分类 fixture；
- Skill 不包含具体答案；
- JSON 修复和 protocol error。

### `test_memory.py`

- add 产生 v1；
- update 产生 v2；
- v1 保留；
- session snapshot 可加载；
- conversation 隔离。

### `test_skills.py`

- Construction/Access 分侧；
- accepted 产生新 Bank；
- rejected 不进入 Active；
- test 模式不能写 Bank。

### `test_workflows.py`

使用 `tiny_locomo.json` 和 MockClient 跑通：

```text
use
train failure
forced replay
natural replay
evaluate base
evaluate mim
```

smoke fixture 建议包含状态更新：

```text
Alice 原来住 Boston
→ 后来搬到 Seattle
→ 问当前城市
→ 问历史城市
```

---

## 20. 实施顺序

### Milestone 1：Base 可用

实现：

- config/schema；
- ModelClient；
- Memory/retrieval；
- Construction Agent；
- Access & Answer Agent；
- Use/Evaluate Workflow；
- LoCoMo loader/metrics。

验收：

```powershell
python main.py evaluate --mode base --split-name test
```

能在一个 conversation 上完整运行并产生 summary。

### Milestone 2：维护侧

实现：

- Failure Agent；
- Skill-Maker；
- Skill Bank；
- Train Workflow；
- failure/candidate artifacts。

验收：

- fixture 中 Construction/Access failure 能正确路由；
- 能产生 Candidate；
- accepted Candidate 生成新 Bank。

### Milestone 3：Replay

实现：

- Construction/Access replay；
- forced/natural；
- 最小 train regression。

验收：

- Candidate 未通过不能 Active；
- 能区分 not_retrieved 和 retrieved_but_failed；
- 旧 Bank 不被覆盖。

### Milestone 4：完整评测

实现：

- 固定 6:2:2；
- validation 选择；
- test freeze；
- 四个 MVP 实验组。

验收：

- 无 conversation 泄漏；
- test 不调用维护侧；
- Base/MiM 预算相同；
- 产生最终对比表。

---

## 21. 开发人员必须避免

1. 不要让强模型参与 Base/Test 正常问答；
2. 不要给 MiM 更多 action steps；
3. 不要把 reference answer 写进 Memory；
4. 不要在 validation/test 更新 Skill；
5. 不要让 Candidate 跳过 Replay；
6. 不要覆盖旧 Memory/Skill 版本；
7. 不要把具体失败答案写进 Skill；
8. 不要把四个 Agent 合成一个无法维护的大 prompt；
9. 不要为 GPT/Qwen/Claude 分叉三套 Agent 逻辑；
10. 不要在 `main.py` 堆积 Workflow 业务代码；
11. 不要为当前 10 个 conversation 引入数据库；
12. 不要在不同实验组使用隐藏的不同默认配置。

---

## 22. 最终验收清单

### 结构

- [ ] 四个 Agent 各自独立、命名清楚；
- [ ] Use/Train/Evaluate 三个 Workflow 独立；
- [ ] 一个 CLI 入口；
- [ ] Prompt、配置、代码和输出分离；
- [ ] 没有数据库、服务化和多进程依赖。

### 模型

- [ ] 运行侧与维护侧模型配置分离；
- [ ] GPT 接口可用；
- [ ] Qwen OpenAI-compatible 接口可用；
- [ ] Claude Anthropic 接口可用；
- [ ] Mock 接口可用；
- [ ] Agent 不依赖厂商原生 tool calling。

### Agent

- [ ] Construction 支持 search/add/update/finish；
- [ ] Access 支持 semantic/keyword/temporal 与 answer；
- [ ] Failure 输出四类归因和证据；
- [ ] Skill-Maker 支持三字段 Draft、CREATE/READ/UPDATE/Tombstone/REUSE；
- [ ] Candidate 必须通过自然检索 Gate，未命中时只修改 description；
- [ ] Construction Skill-Maker 只迭代 Failure 定位的第一个断点；
- [ ] Other/Invalid 不学习 Skill。

### Workflow

- [ ] `use` 能导入 conversation 并回答单个问题；
- [ ] `train` 能从 train failure 学 Skill；
- [ ] `evaluate` 能运行 Base/MiM；
- [ ] forced/natural replay 都存在；
- [ ] test 不调用维护侧。

### 数据

- [ ] 每个 session 保存 Memory 快照；
- [ ] update 保留历史版本；
- [ ] Skill Bank 保存版本；
- [ ] rejected Candidate 可追溯；
- [ ] 6:2:2 按 conversation；
- [ ] split、config、prompt 和 dataset hash 可追溯。

### 实验

- [ ] Base/MiM runtime 弱模型相同；
- [ ] 工具、top-k 和 max steps 相同；
- [ ] 输出 LoCoMo-compatible F1；
- [ ] 输出 failure/replay/skill 指标；
- [ ] 输出 runtime/maintenance 成本；
- [ ] smoke test 无 API key 可运行。

---

## 23. 最终判断

这个版本的复杂度是合适的：

- 四个 Agent 清楚存在，没有为了“最小”而混成两个大模块；
- Workflow 清楚覆盖使用、训练和评测；
- 模型接口支持运行侧弱模型、维护侧强模型；
- GPT、Qwen、Claude 能共享核心逻辑；
- Memory/Skill 使用文件版本，足以溯源但不引入数据库；
- 项目树能让新代码人员快速定位职责；
- Base 能独立运行；
- MiM 的变量仅是 Skill；
- 能完成 LoCoMo 6:2:2 的完整实验闭环。

开发实现时应坚持：

```text
功能最少
边界清楚
结果可复现
代码可阅读
关键失败可追溯
```

不再继续增加基础设施，除非实际评测过程中出现明确需求。

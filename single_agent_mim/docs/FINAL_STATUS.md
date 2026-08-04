# MiM 代码与工作状态文档 (2026-08-04)

## 一、架构

```
bank0(无Skill) → nsc_train运行时 → judge(C/P/I) → diagnosis(3阶段)
→ candidates生成 → [V1:K-means CRUD | V2:Summarizer+CRUD] → Skill Bank
→ evaluation(并行构建+答题) → LLM-as-Judge → 对比Bank0
```

## 二、评测结果

### 最终对比（同一 Bank0 基线重跑）

| | Skills | C | P | I | C+P | Token-F1 |
|---|---|---|---|---|---|---|
| Bank0(基线) | 0 | 167 | 65 | 160 | 59.2% | 36.26% |
| Access-only | 23A | 177 | 54 | 161 | 58.9% | 35.02% |
| **Cons-only** | **26C** | **183** | 60 | **149** | **62.0%** | 35.87% |
| Full | 23A+26C | 174 | 68 | 150 | 61.7% | 36.97% |

### 分题型 C+P

| 题型 | Bank0 | Access | Cons | Full | 最佳 |
|---|---|---|---|---|---|
| Temporal | 43.8% | 46.9% | **59.4%** | 54.7% | Cons +15.6pp |
| Adversarial | 36.4% | 44.3% | 45.5% | 45.5% | Cons/Full +9.1pp |
| Multi-hop | 71.2% | 66.7% | 70.5% | 72.4% | Full +1.2pp |
| Single-hop | 81.0% | 77.8% | 74.6% | 74.6% | Bank0 |
| Open-domain | 47.6% | 42.9% | 38.1% | 33.3% | Bank0 |

### 核心结论

1. **Cons Skill (26C) 独自最优**：C+P=62.0%，C=183，均超 Bank0 和 Full。Temporal +15.6pp。
2. **Access Skill 拖累简单题**：Single-hop -3.2pp, Open-domain -4.7pp, Multi-hop -4.5pp。
3. **Cons+Access 互补**：Full F1 最高(36.97%) 但 C+P 不如 Cons-only，说明 Access 把正确变部分正确。
4. **Open-domain 是所有方法的天敌**，Bank0 47.6% 最高。

## 三、Prompt 文件

| 文件 | 用途 |
|---|---|
| `prompts/skill_maker/candidate_generation_access.md` | Access 候选生成 |
| `prompts/skill_maker/candidate_generation_construction.md` | Cons 候选生成（强调窄触发） |
| `prompts/skill_maker/batch_crud_access.md` | Access CRUD（合并相同检索策略） |
| `prompts/skill_maker/batch_crud_construction.md` | Cons CRUD（保守合并，防过度提取） |
| `prompts/skill_maker/cluster_summarizer_access.md` | Access Summarizer（1-5 drafts/cluster，简练优先） |
| `prompts/skill_maker/cluster_summarizer_construction.md` | Cons Summarizer（同上） |
| `prompts/skill_maker/candidate_generation.md` | 旧版（保留兼容） |
| `prompts/skill_maker/batch_crud.md` | 旧版（保留兼容） |

## 四、核心代码改动

### `src/mim/`

| 文件 | 改动 |
|---|---|
| `config.py` | 新增 6 个 prompt 路径字段（access/cons 分离 + summarizer） |
| `agents/skill_learning.py` | CRUD max_tokens 放大；duplicate skill_id 改为自动生成新ID |
| `skill_maker/batch.py` | 删 `refine()`；`cluster()` 直出 K-means；`retrieve()` 简化为 per-candidate top-2 |
| `skill_maker/cluster_v2.py` | **新建** — 递归 K-means 聚类 + max_cluster_size 切分 |
| `skill_maker/pipeline.py` | `refine(cluster(...))` → `cluster(...)` |
| `skill_maker/validator.py` | name 80→100, desc 320→400, content 1200→2500 |
| `workflows/train.py` | 4个Agent(access/cons分离)；candidate 生成并行化 |
| `retrieval/embedder.py` | `local_files_only` 优先（HF 不可用时离线工作） |

### `scripts/`

| 文件 | 用途 |
|---|---|
| `run_skill_bank_pipeline.py` | V1 pipeline（现有 K-means CRUD） |
| `run_skill_bank_pipeline_v2.py` | V2 pipeline（Summarizer + 分批 CRUD） |
| `run_parallel_eval.py` | 并行评测（2路构建，答题尚为顺序） |
| `judge_predictions.py` | LLM-as-Judge（支持 --workers 12） |

### `configs/`

- `qwen3_8b_dashscope.yaml`：runtime=qwen3-8b@DashScope, maintenance=deepseek-v4-flash@DeepSeek (3 keys)

## 五、数据位置

| 目录 | 内容 |
|---|---|
| `outputs/nsc_train/` | Bank0 无 skill 运行时数据（6 convs） |
| `outputs/judge/` | Judge 结果 |
| `outputs/bank1_new_diag/` | 最新 diagnosis（114A+203C） |
| `outputs/bank1_new_v5/skills/candidates/` | V1 生成的 candidates（110A+203C） |
| `outputs/bank1_draft_crud_v2/` | V2 最新产出（23A+26C） |
| `outputs/bank0_val_rerun/` | Bank0 重跑（并行构建 + 顺序答题） |
| `outputs/acc_final/` | Access-only 快速评测（复用 DB，8路并行答题） |
| `exp/single-agent/bank1_new/banks/` | V1 结果（83A+103C） |
| `exp/single-agent/bank1_draft_crud_v2/banks/` | V2 结果（23A+26C） |

## 六、已知问题

1. **Access Skill 描述过宽**：语义检索容易误触发，导致简单题被复杂搜索策略干扰
2. **Construction Skill 仍有冗长者**：个别 content 超 10 条（应 ≤4）
3. **评测答题串行**：`run_parallel_eval.py` 构建并行但答题顺序
4. **复用 DB 需手写脚本**：MiMRuntime 默认走 `ingest()` 重建，需先建目录+复制DB再绕过

## 七、下一步

1. **提升 Access Skill 质量**：description 加失败条件（"当第一次搜索返回空时..."），减少误触发
2. **Cons Skill 单独上线**：26C 已经是最优单一方案
3. **答题并行化**：改 `run_parallel_eval.py` 支持 8 路并行答题
4. **Open-domain 专项**：所有方法在这个题型上均退化，需单独分析

# Memory in Memory (MiM) 项目报告

## 项目简介

MiM 是一个面向大模型记忆系统的**错误驱动元记忆层**。当下游问答出错时，MiM 诊断错误来源（记忆构建侧/记忆访问侧），将修复经验抽象为可复用的自然语言 Skill，注入运行时指导 Agent 改进记忆构建和访问行为。

核心流程：**诊断错误 → 生成候选 Skill → 聚类总结 → CRUD 合并 → 注入评测**

## 系统架构

```
                    ┌─────────────────────┐
                    │   Diagnosis (3阶段)   │
                    │ answer→access→cons   │
                    └─────────┬───────────┘
                              │ candidates
                    ┌─────────▼───────────┐
                    │  V2 Summarizer       │
                    │  K-means聚类→1-5草稿 │
                    └─────────┬───────────┘
                              │ drafts
                    ┌─────────▼───────────┐
                    │  Per-draft CRUD      │
                    │  分批(≤8)→Bank       │
                    └─────────┬───────────┘
                              │
              ┌───────────────┼───────────────┐
              ▼                               ▼
    ┌─────────────────┐             ┌─────────────────┐
    │ Access Skill Bank│             │ Cons Skill Bank  │
    │ (检索策略指导)    │             │ (记忆构建指导)    │
    └────────┬────────┘             └────────┬────────┘
             │                               │
             └───────────┬───────────────────┘
                         ▼
              ┌─────────────────────┐
              │   Runtime (Qwen3-8B) │
              │   记忆构建 + 问答     │
              └─────────────────────┘
```

## 关键技术决策

1. **Prompt 物理分离**：Access 和 Construction 使用独立的候选生成、CRUD、Summarizer 提示词
2. **V2 Summarizer 替代直接 CRUD**：先 K-means 聚类，每簇总结 1-5 个草稿，再分批 CRUD，避免碎片化（83→23 Skills）
3. **复用已有记忆**：评测时先创建目录+复制 DB，再用 `MiMRuntime` 打开（`CREATE TABLE IF NOT EXISTS` 不覆盖），跳过重建
4. **并行评测**：8 路并行答题，12 路并行 Judge

## 文件结构

```
single_agent_mim/
├── configs/
│   └── qwen3_8b_dashscope.yaml    # 主配置：Qwen3-8B Runtime + DeepSeek-Flash Maintenance(3 keys)
├── prompts/skill_maker/
│   ├── candidate_generation_access.md       # Access 候选生成
│   ├── candidate_generation_construction.md # Cons 候选生成
│   ├── batch_crud_access.md                 # Access CRUD
│   ├── batch_crud_construction.md           # Cons CRUD
│   ├── cluster_summarizer_access.md         # Access 聚类总结
│   └── cluster_summarizer_construction.md   # Cons 聚类总结
├── src/mim/
│   ├── agents/skill_learning.py   # CRUD Agent (max_tokens修复, dup ID修复)
│   ├── skill_maker/
│   │   ├── batch.py               # K-means聚类, Batch检索, CRUD执行
│   │   ├── cluster_v2.py          # 递归K-means聚类 (新建)
│   │   ├── pipeline.py            # 聚类→CRUD 管道
│   │   └── validator.py           # Skill长度校验 (放宽)
│   ├── workflows/
│   │   ├── train.py               # 训练主流程 (Agent分离+并行candidate)
│   │   └── evaluate.py            # 评测 (原有)
│   └── retrieval/embedder.py      # local_files_only优先
├── scripts/
│   ├── run_skill_bank_pipeline.py     # V1 pipeline
│   ├── run_skill_bank_pipeline_v2.py  # V2 pipeline (Summarizer+CRUD)
│   ├── run_parallel_eval.py           # 并行评测
│   └── judge_predictions.py           # LLM-as-Judge (12 workers)
└── outputs/
    ├── nsc_train/                 # Bank0 无Skill 运行时数据
    ├── bank1_new_diag/            # 最新 diagnosis
    ├── bank1_new_v5/skills/       # V1 candidates (110A+203C)
    ├── bank1_draft_crud_v2/       # V2 产出 (23A+26C)
    ├── bank0_val_rerun/           # Bank0 重跑基线
    └── acc_final/                 # Access-only 快速评测
```

## 评测结果

### 主表 (vs Bank0 同一基线)

| 变体 | Skills | C | P | I | C+P | Token-F1 |
|---|---|---|---|---|---|---|
| Bank0 | 0 | 167 | 65 | 160 | 59.2% | 36.26% |
| Access-only | 23A | 177 | 54 | 161 | 58.9% | 35.02% |
| **Cons-only** | **26C** | **183** | **60** | **149** | **62.0%** | 35.87% |
| Full | 23A+26C | 174 | 68 | 150 | 61.7% | 36.97% |

### 分题型 C+P

| 题型 | Bank0 | Access | Cons | Full |
|---|---|---|---|---|
| Temporal | 43.8% | 46.9% | **59.4%** | 54.7% |
| Adversarial | 36.4% | 44.3% | 45.5% | 45.5% |
| Multi-hop | 71.2% | 66.7% | 70.5% | 72.4% |
| Single-hop | 81.0% | 77.8% | 74.6% | 74.6% |
| Open-domain | 47.6% | 42.9% | 38.1% | 33.3% |

## Skill 召回统计

| 版本 | Skills | 实际使用 | 总被选次数 | 平均 |
|---|---|---|---|---|
| V1 Access (83A) | 83 | 59 (71%) | 217 | 2.6/skill |
| V1 Full (83A+103C) | 186 | 59 access | 218 | — |
| V2 Full (23A+26C) | 49 | 20 access (87%) | 290 | 14.5/skill |
| Draft_v1 (26A+32C) | 58 | 22 access (85%) | 252 | 11.5/skill |

V2 每 skill 被使用 14.5 次（V1 仅 2.6 次），说明合并后的 skill 更通用、检索命中率更高。

## 版本溯源

所有版本的 Skill Bank 均保存在 `exp/single-agent/` 下：

| 版本 | 路径 | 来源 |
|---|---|---|
| Bank0(基线) | `exp/single-agent/bank0/` | Qwen3-8B 无 Skill 运行时 |
| V1 K-means | `exp/single-agent/bank1_new/banks/` | 原始 K-means CRUD |
| V2 Draft | `exp/single-agent/bank1_draft_crud_v2/banks/` | Summarizer + 分批CRUD |
| V2 Access | `exp/single-agent/bank1_draft_crud_v2_access/banks/` | 同上 Access-only |
| Draft_v1 | `exp/single-agent/bank1_draft_crud_v1/banks/` | 旧版 Summarizer |

评测结果（qa_results.jsonl + Judge summary）均保存在对应 `outputs/` 目录。

## 已知局限

1. **Access Skill 过度指导**：简洁题被复杂搜索策略干扰（Single-hop -3.2pp）
2. **Open-domain 全退化**：所有方法均低于 Bank0，需专项优化
3. **Cons Skill 仍有冗长**：部分 content 超 10 条
4. **评测答题顺序**：`run_parallel_eval.py` 构建并行但答题串行

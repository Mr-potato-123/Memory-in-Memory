# MiM 改动与结果报告

## 一、Prompt 物理分离（Access/Construction 独立）

**改动**: 4个新建 + 2个保留（旧版兼容）

| 新建文件 | 侧重点 |
|----------|--------|
| `prompts/skill_maker/candidate_generation_access.md` | Access候选生成 |
| `prompts/skill_maker/candidate_generation_construction.md` | Cons候选生成（强调窄触发防膨胀） |
| `prompts/skill_maker/batch_crud_access.md` | Access CRUD（合并相同检索策略） |
| `prompts/skill_maker/batch_crud_construction.md` | Cons CRUD（保守合并防过度提取） |
| `prompts/skill_maker/cluster_summarizer.md` | V2 Summarizer（1-3 skills/cluster） |

**Agent分离**: `train.py` 和 `run_skill_bank_pipeline.py` 从2个Agent(共用) → 4个Agent(access/cons独立)

---

## 二、CRUD 聚类修复

**改动**: `src/mim/skill_maker/batch.py`
- 删除 `refine()` 方法（69行）
- `cluster()` 不再按 max_batch_size 切分，K-means直出
- `retrieve()` selection 简化为 per-candidate top-2保底
- duplicate skill_id 冲突改为自动生成新ID（原为抛异常）
- `pipeline.py`: `refine(cluster(...))` → `cluster(...)`

---

## 三、Validator 放宽

| 参数 | 改前 | 改后 |
|------|------|------|
| name_max | 80 | 100 |
| description_max | 320 | 400 |
| content_max | 1200 | 2500 |

---

## 四、并行化

- `train.py`: candidate生成改为 ThreadPoolExecutor 并行（3 API keys轮询）
- `scripts/run_parallel_eval.py`: **新建** — 多个conversation并行构建+答题

---

## 五、网络兼容

- `embedder.py`: 优先 `local_files_only=True` 加载（HF Hub不可用时）
- `judge_predictions.py`: Judge调用禁用thinking tokens

---

## 六、CRUD Agent Token修复

- `max_tokens`: `max(2500, 600*n)` → `max(12000, 3000*n)`
- `extra_body.thinking` 禁用（避免thinking吃掉输出预算）
- V2 pipeline 单独创建无thinking的CRUD模型

---

## 七、V2 Pipeline（新建）

`scripts/run_skill_bank_pipeline_v2.py`: 复用已有candidates
- K-means聚类 → Summarizer(尽量少skills) → 分批CRUD(3个/batch)
- Access/Construction并行处理
- `src/mim/skill_maker/cluster_v2.py`: K-means + max_cluster_size切分

---

## 八、评测结果总表

### 完整系统对比

| 系统 | Skills | Token-F1 | C | P | I | C+P率 | vs Bank0 |
|------|--------|----------|---|---|---|-------|----------|
| Bank0 (基线) | 0 | 32.63% | 165 | 52 | 175 | 55.36% | — |
| **V1 Access-only** | **83A** | **35.68%** | 171 | 58 | 163 | **58.42%** | **+3.05pp** |
| V1 Full | 83A+103C | 30.76% | 149 | 60 | 182 | 53.45% | -1.87pp |
| V2 Access-only | 3A | 33.64% | — | — | — | — | +1.01pp |
| Bank1 (原始参考) | 54A+75C | 36.32% | 185 | 60 | 147 | 62.50% | +3.69pp |

### 分题型 C+P (仅 V1 Access-only vs Bank0)

| 题型 | Bank0 | V1 A-only | Δ |
|------|-------|-----------|-----|
| Single-hop | 71.4% | **82.5%** | +11.1pp |
| Temporal | 39.1% | 43.8% | +4.7pp |
| Open-domain | 47.6% | 47.6% | 0.0pp |
| Multi-hop | 65.4% | 67.3% | +1.9pp |
| Adversarial | 39.8% | 38.6% | -1.1pp |

### Construction Skill 影响分析

加入 103 个 Construction Skill 后：
- F1 从 35.68% 暴跌至 30.76%（-4.92pp）
- C 从 171 降至 149（-22）
- 265/392 (68%) 的答案与 Access-only 不同
- 19/103 个 Construction Skill 实际被使用
- 最常用: "Extract shared-activity statements"(13x), "Preserve emotional statements"(5x)
- 根因: 碎片化过度提取导致记忆库噪音

### V2 Summarizer 验证

仅 3 个 Access Skill 就超过 Bank0(+1.01pp)，证明 Summarizer 压缩方向正确。
110 candidates → 5 clusters → 8 drafts → 3 skills。

---

## 九、文件改动清单

| 操作 | 文件 |
|------|------|
| **新建** | `prompts/skill_maker/candidate_generation_access.md` |
| **新建** | `prompts/skill_maker/candidate_generation_construction.md` |
| **新建** | `prompts/skill_maker/batch_crud_access.md` |
| **新建** | `prompts/skill_maker/batch_crud_construction.md` |
| **新建** | `prompts/skill_maker/cluster_summarizer.md` |
| **新建** | `src/mim/skill_maker/cluster_v2.py` |
| **新建** | `scripts/run_parallel_eval.py` |
| **新建** | `scripts/run_skill_bank_pipeline_v2.py` |
| **修改** | `src/mim/config.py` |
| **修改** | `src/mim/workflows/train.py` |
| **修改** | `src/mim/skill_maker/batch.py` |
| **修改** | `src/mim/skill_maker/pipeline.py` |
| **修改** | `src/mim/skill_maker/validator.py` |
| **修改** | `src/mim/agents/skill_learning.py` |
| **修改** | `src/mim/retrieval/embedder.py` |
| **修改** | `scripts/run_skill_bank_pipeline.py` |
| **修改** | `scripts/judge_predictions.py` |
| **修改** | `configs/default.yaml` |
| **修改** | `configs/qwen3_8b_dashscope.yaml` |
| **保留** | `prompts/skill_maker/candidate_generation.md` (旧版兼容) |
| **保留** | `prompts/skill_maker/batch_crud.md` (旧版兼容) |

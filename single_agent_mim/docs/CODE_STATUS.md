# MiM 代码现状说明 (2026-08-03)

## 1. 架构概览

```
单次评测流程:
  nsc_train (bank0 无skill运行时) → judge (C/P/I) → diagnosis (3阶段) 
  → candidate生成 → CRUD → Skill Bank → evaluation → judge对比

两条CRUD路线:
  V1 (K-means): candidates → K-means分簇 → 每簇独立CRUD → skills
  V2 (Summarizer): candidates → K-means分簇 → Summarizer总结 → 分批CRUD → skills
```

## 2. 当前文件结构

### Prompt 文件 (`prompts/skill_maker/`)

| 文件 | 用途 |
|------|------|
| `candidate_generation_access.md` | Access Candidate生成提示词 |
| `candidate_generation_construction.md` | Construction Candidate生成（强调窄触发、防记忆膨胀） |
| `batch_crud_access.md` | Access CRUD提示词（合并相同检索策略） |
| `batch_crud_construction.md` | Construction CRUD提示词（保守合并、防过度提取） |
| `cluster_summarizer.md` | V2 Summarizer提示词（1-3 skills/cluster，同机制合并） |
| `candidate_generation.md` | 旧版（已废弃，保留兼容） |
| `batch_crud.md` | 旧版（已废弃，保留兼容） |

### 核心代码 (`src/mim/`)

| 文件 | 改动说明 |
|------|----------|
| `agents/skill_learning.py` | CRUD max_tokens改为 max(12000, 3000*n)；删thinking debug日志 |
| `skill_maker/batch.py` | 删 `refine()` 方法；`cluster()` 不再按 max_batch_size 切分；`retrieve()` 简化selection为 per-candidate top-2 |
| `skill_maker/cluster_v2.py` | **新建** — V2聚类：K-means分簇 + 大簇切分(max 25) |
| `skill_maker/pipeline.py` | `refine(cluster(...))` → `cluster(...)` |
| `skill_maker/validator.py` | name_max 80→100, desc_max 320→400, content_max 1200→2500 |
| `workflows/train.py` | 4个Agent实例(access/cons × candidate/crud)；candidate生成改为并行ThreadPoolExecutor |
| `config.py` | PromptsConfig：2个prompt字段→4个(access/cons分离) |
| `retrieval/embedder.py` | 添加 local_files_only 优先加载（HF不可用时离线工作） |

### 脚本 (`scripts/`)

| 文件 | 用途 |
|------|------|
| `run_skill_bank_pipeline.py` | **V1 pipeline** — 从diagnosis生成candidates → K-means CRUD → 导出bank |
| `run_skill_bank_pipeline_v2.py` | **V2 pipeline** — 复用已有candidates → Summarizer → 分批CRUD → 导出bank |
| `run_parallel_eval.py` | **新建** — 并行评测：多个conversation独立构建+答题 |
| `judge_predictions.py` | LLM-as-Judge（修复了thinking tokens问题） |

### 配置 (`configs/`)

| 文件 | 改动 |
|------|------|
| `default.yaml` | prompts段4个路径 |
| `qwen3_8b_dashscope.yaml` | prompts段4个路径；runtime=qwen3-8b@DashScope；maintenance=deepseek-v4-flash@DeepSeek(3 API keys轮询) |

## 3. 当前数据位置

### 原始数据
- `outputs/nsc_train/` — bank0无skill运行时数据（6个conversation）
- `outputs/judge/` — Judge结果（C/P/I标签）

### Diagnosis数据
- `outputs/bank1_new_diag/` — 最新diagnosis（V3三步，688 QA，114A+203C packages）

### Candidate数据
- `outputs/bank1_new_v5/skills/candidates/` — V1生成的candidates（110A+203C），可被V2复用

### Skill Bank
- `exp/single-agent/bank1_new/banks/` — V1 K-means结果：83A+103C (F1: A-only=35.68%, Full=30.76%)
- `exp/single-agent/bank1_new_v2/banks/` — V2 Summarizer结果：3A+0C (F1: 33.64%)
- `exp/single-agent/bank0/` — 基线（无skill）：F1=32.63%, C+P=55.36%
- `exp/single-agent/bank1/` — 原始Bank1：54A+75C, F1=36.32%, C+P=62.50%

### 评测结果
- `outputs/bank1_new_eval_access/` — V1 Access-only评测（F1=35.68%）+ Judge结果
- `outputs/bank1_new_eval_full/` — V1 Full评测（F1=30.76%）+ Judge结果
- `outputs/bank1_new_v2_eval/` — V2评测（F1=33.64%）

## 4. 已知问题

### V1 (K-means CRUD)
1. **Construction Skill有害**: 103个碎片化Construction Skill导致F1从35.68%跌到30.76%
   - 根因：CRUD合并不足，19/103个被使用但过度提取"preserve every X"
2. **Skill数量偏多**: 83A+103C vs Bank1的54A+75C

### V2 (Summarizer + CRUD)
1. **Construction CRUD失败**: API返回空响应（thinking tokens或payload过大）
2. **第二批Access CRUD失败**: 第一批成功(v001)后，第二批看到已有skill导致CRUD异常
3. **压缩过猛**: 110 candidates → 3 skills（目标12-15个skills）
4. **target_cluster_size参数无效**: 无论传8还是12，始终产生5+9个簇

### 通用
1. **HF Hub不稳定**: embedder需用local_files_only
2. **DeepSeek API连接波动**: summarizer经常Connection error
3. **thinking tokens冲突**: CRUD必须禁用thinking（extra_body={}, reasoning_effort=None）

## 5. 结果汇总

| 系统 | Skills | F1 | C+P | vs Bank0 |
|------|--------|-----|-----|----------|
| Bank0 | 0 | 32.63% | 55.36% | — |
| V1 Access-only | 83A | **35.68%** | 58.42% | +3.05pp |
| V1 Full | 83A+103C | 30.76% | 53.45% | -1.87pp |
| V2 Access-only | 3A | 33.64% | — | +1.01pp |
| Bank1 (参考) | 54A+75C | 36.32% | 62.50% | +3.69pp |

## 6. 下一步建议

1. **V2方向验证成功**: 3个skill就超过bank0，Summarizer压缩有效
2. **增加簇数**: 修复target_cluster_size参数，让access从5簇→14簇，得~40 drafts→~15 skills
3. **修复Cons CRUD**: 排查construction payload过大导致空响应
4. **修复多批次CRUD**: 第一批次成功后，后续批次bank版本变化导致失败
5. **并行Evaluation**: `run_parallel_eval.py`已支持并行评测，比顺序快2倍

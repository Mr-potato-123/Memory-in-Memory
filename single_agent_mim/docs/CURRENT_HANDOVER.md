# MiM 当前交接文档

> 更新时间：2026-08-11  
> 当前代码提交：`e47f3b6`（已推送到 `origin/main`）  
> 当前结论：**保留 `b1_joint_full_iter_high` 产生的 Bank1 作为主 Bank；其后的已测衍生 Bank 均未稳定超过它。**  
> 本文是当前状态的权威入口。`HANDOVER.md`、`HANDOVER_PROBLEMS.md` 和早期迭代报告记录了历史过程，其中部分路径已清理、数字属于旧运行，不应直接作为当前结论。

---

## 1. 项目在做什么

MiM（Memory in Memory）是一个让长对话记忆系统从自身运行经验中形成可复用 Skill 的实验系统。

它不是单纯的“失败轨迹学习”，当前目标是同时内化三类经验：

1. **失败经验**：回答错误后，定位是回答行为、记忆访问还是记忆构建出了问题，再生成修复 Skill。
2. **正反差分经验**：比较同一题在两个 Bank 下的正确/错误轨迹，学习为什么发生 C→W、W→C 或持续 W→W。
3. **纯成功经验**：从无 Skill 仍答对的完整运行轨迹中，只提炼非平凡、可复用的 Access 恢复策略；简单直查成功只用于保护默认策略，不生成 Skill。

最终 Runtime 可见的 Skill 始终保持简洁：

```text
name
description       # 可观察、窄范围的触发条件
content[]         # 简短可执行指令
```

训练和维护元数据（来源案例、transition、failure age、maintenance intent 等）放在 Candidate/事务层，不进入 Runtime Skill 正文。

### 1.1 双侧结构

| 侧 | 作用 |
|---|---|
| Access Skill | 指导首次默认检索之后的恢复检索、证据组合、核验和回答策略；Answer failure 也映射到 Access Skill |
| Construction Skill | 仅作为会话记忆提取的可选参考；运行时固定采用 ADD/精确重复 SKIP，不允许 Skill 发出存储 CRUD |

Runtime 使用 qwen3-8b；Judge、诊断、Candidate 生成和 CRUD 维护主要使用 deepseek-v4-flash。数据集是 LoCoMo swap split：

- Train：`conv-30/42/43/44/47/50`，共 1159 题
- Validation：`conv-26/41`，共 392 题
- Test：`conv-48/49`，共 435 题

配置入口是 `configs/qwen3_8b_swap_b0.yaml`。配置中已有本地 API 凭据，这是用户明确允许的本地私有仓库方案；本文不复制凭据内容。

---

## 2. 当前应遵循的迭代流程

### 2.1 第一类：全量迭代

```text
Bank N
  → 在 Train 六个对话上重新构建记忆并回答 1159 题
  → binary Judge
  → 对 W 题运行标准 Answer / Access / Construction 诊断
  → 构建 Judge-C 的 Skill 成功使用样例
  → 构建无 Skill 正确题的 success package v2
     （回答路径 + 检索动作 + 最终证据 + 被引用记忆的构建过程）
  → 分片生成失败 Candidate 和纯成功 Access Candidate
  → 合并 Candidate
  → 聚类、草稿、CRUD 到 Bank N 的 shadow 副本
  → 发布 Bank N+1
  → 只在 Val/Test 上评测，不从 Val/Test 学习
```

主要编排入口：`scripts/run_full_iter_b0.py`。它现在支持空 Bank 或通过 `--initial-bank` 指定已有 Bank。

### 2.2 第二类：正反案例分析迭代

必须先在 **Train** 上分别运行 Bank N 和 Bank N+1，再按相同 `qa_id` 比较 Judge 标签：

| 转换 | 是否学习 | 含义 |
|---|---|---|
| C→W | 是 | 新 Bank 引入回归，应学习保留/避免机制 |
| W→C | 是 | 新 Bank 修复了错误，应学习采用/保持机制 |
| W→W | 是 | 上一轮仍未修好，携带 `failure_age` 和 repair lineage 继续诊断 |
| C→C | 否，只计数 | 稳定成功，不进入差分诊断和 Candidate 来源 |

`scripts/build_contrastive_pairs.py` 生成 `iteration_cases_v3`。C→W/W→C 使用正确侧与错误侧的双轨迹；W→W 使用 prior/current 两条错误轨迹、gold source path 和历史 repair lineage。

正反诊断先生成一个共享的 claim-level core，再确定性投影为隔离的 Access/Construction 包：

```text
reference claims
  ├─ correct/prior side: memory coverage / retrieval coverage / answer coverage
  ├─ wrong/current side: memory coverage / retrieval coverage / answer coverage
  └─ attribution: answer / access / construction
```

归因规则：

- Answer 必须单独存在；必要记忆和检索在两侧都完整时，才允许归因 Answer。
- Access 与 Construction 可以同时存在，也可以作用在不同 claim 上。
- Answer 投影到 Access Candidate generator，因为回答策略属于 Access Skill。
- 正样本直接学习只允许生成 Access Skill，不生成 Construction Skill。

主要入口：

- `scripts/build_contrastive_pairs.py`
- `scripts/run_flip_diagnosis.py`
- `src/mim/agents/flip_failure.py`
- `scripts/run_candidates_from_diagnosis.py`
- `scripts/run_skill_bank_pipeline_v2.py`
- 可选逐案例直接 CRUD：`scripts/run_direct_contrastive_crud.py`

### 2.3 Runtime Skill 注入语义

当前实现与项目原意基本一致：

- Access 必须先执行一次默认搜索，之后才根据“首轮结果缺了什么”检索 Skill。
- 首轮结果已经直接支持完整答案时，应忽略恢复 Skill。
- Access 和 Construction 最终最多各选择 1 个 Skill；虽然配置写 `skill_top_k: 2`，Workflow 中实际使用 `min(1, ...)`。
- Skill 是 learned behavioral prior，不是强制命令；证据和默认工作流优先。
- Construction Skill 对整个 session 生效，必须有严格的不适用边界。

---

## 3. 当前权威 Bank 与结果

### 3.1 当前主 Bank：Bank1

唯一推荐继续作为实验起点的 Bank：

```text
outputs/b1_joint_full_iter_high/bank/bank1_joint/skills/published_bank1_full
```

规模：

- Access：65
- Construction：34
- 合计：99

生成过程保存在 `outputs/b1_joint_full_iter_high/`：

- `failure_access_skills/`：失败侧 Access Candidate
- `failure_construction_skills/`：失败侧 Construction Candidate
- `success_skills/`：纯成功 Access Candidate
- `joint_candidates/`：合并后的 310 Access + 212 Construction source candidates
- `bank/bank1_joint/`：聚类、草稿、CRUD 和正式发布结果
- `baseline/`：与该 Bank1 对应的空 Bank Val/Test
- `eval/`：Bank1 Val/Test
- `train_bank1_judge/`：Bank1 Train Judge

Pipeline 摘要：48 个 Access 语义簇、29 个 Construction 语义簇，形成 88/44 个草稿，最终发布 65A+34C。

### 3.2 当前同运行族的 Baseline 与 Bank1

以下 Baseline 与 Bank1 位于同一个实验根目录，应作为当前主要比较：

| 版本 | Val | conv-26 | conv-41 | Test | conv-48 | conv-49 |
|---|---:|---:|---:|---:|---:|---:|
| 空 Bank Baseline | 215/392 = 54.85% | 110/199 = 55.28% | 105/193 = 54.40% | 231/435 = 53.10% | 128/239 = 53.56% | 103/196 = 52.55% |
| **Bank1** | **222/392 = 56.63%** | 105/199 = 52.76% | 117/193 = 60.62% | **237/435 = 54.48%** | 132/239 = 55.23% | 105/196 = 53.57% |
| Bank1 - Baseline | **+1.78pp** | -2.51pp | +6.22pp | **+1.38pp** | +1.67pp | +1.02pp |

对应摘要：

- `outputs/b1_joint_full_iter_high/baseline/{val_judge,test_judge}/summary.json`
- `outputs/b1_joint_full_iter_high/eval/{val_judge,test_judge}/summary.json`

### 3.3 Bank1 后续衍生版本

| 版本 | 来源 | Skill 数 | Val | Test | 结论 |
|---|---|---:|---:|---:|---|
| **Bank1** | 全量失败 + 成功经验联合迭代 | 65A+34C | **56.63%** | **54.48%** | 当前主 Bank |
| Bank_NEW | 复用旧 C→W/W→C Candidate，排除 W→W，基于 Bank1 CRUD | 83A+40C | 54.08% | 50.80% | 退化，不作为新基座 |
| Bank2 full | 从 Bank1 做真正全量失败+成功联合迭代 | 89A+60C | 55.10% | 50.34% | 退化，不发布为主 Bank |
| Three-iter Bank2 | 早一轮 C→W/W→C/W→W 流程 | 未统一作为主规模 | 54.08% | 53.10% | 未超过 Bank1 |
| Three-iter Bank3 | 下一轮持续迭代 | 未统一作为主规模 | 55.10% | 49.89% | Test 明显退化 |
| Direct CRUD branch | 逐案例直接 CRUD Bank1 | 历史分支 | 54.85% | 53.33% | 未超过 Bank1 |
| Earlier contrastive branch | 较早的正反案例分支 | 历史分支 | 52.30% | 56.32% | Test 高但 Val 低，且不应与当前主链混为一组 |

最近两个衍生版本的逐对话结果：

| 版本 | conv-26 | conv-41 | conv-48 | conv-49 |
|---|---:|---:|---:|---:|
| Bank_NEW | 52.26% | 55.96% | 51.88% | 49.49% |
| Bank2 full | 50.75% | 59.59% | 51.88% | 48.47% |

Bank_NEW 产物：

```text
outputs/bank_new_20260811/
outputs/bank_new_val*
outputs/bank_new_test*
```

Bank1 全量衍生 Bank2 产物：

```text
outputs/bank1_full_after_no_w2w_20260811/
outputs/bank1_full_after_no_w2w_val*
outputs/bank1_full_after_no_w2w_test*
```

---

## 4. 已经生成、可以直接复用的产物

### 4.1 Baseline Train 与标准诊断

最终保留的完整空 Bank 两阶段运行：

```text
outputs/empty_bank_two_phase_full_v3_20260809/
```

关键内容：

- `train/`：空 Bank Train 运行产物
- `judge/`：Train binary Judge，C=610/W=549
- `diagnosis/answer_failure/`：196 个 repair packages
- `diagnosis/access_failure/`：101 个 repair packages
- `diagnosis/cons_failure/`：332 个 repair packages
- `success_package.jsonl`：610 条历史成功索引

另有本轮系统使用的空 Bank 原始运行目录：`outputs/b1_joint_train_empty*`。

### 4.2 Bank1 Train 与翻转输入

```text
outputs/b1_joint_train_bank1*
outputs/b1_joint_full_iter_high/train_bank1_judge/
outputs/b1_joint_full_iter_high/iteration_cases_b0_to_b1_joint.json
```

Bank1 Train Judge 一次结果为 C=615/W=544；同一 Bank1 在后续全量实验中重跑得到 C=609/W=550。两次仅差 6 题，说明 Runtime 执行仍有小幅随机波动，跨时间比较必须谨慎。

`iteration_cases_b0_to_b1_joint.json` 已包含：

- C→W：120
- W→C：145
- W→W：424
- C→C：470（只计数）

### 4.3 已保存的正反 Candidate

Bank_NEW 使用的 C→W/W→C Candidate 已经保存，不需要重新诊断或重新生成：

```text
outputs/bank_new_20260811/skills/candidates/
```

共 196 个 Candidate 文件：

- Access：118
- Construction：78

它们来自早一轮保存的正反 Candidate，明确排除了 W→W，用于“无 W→W”消融。该版本已经完成 CRUD 和 Val/Test，结果退化。

完整三轮正反流程和中间结果位于：

```text
outputs/three_iter_20260811/
outputs/three_iter_20260811_*
```

### 4.4 Bank1 全量迭代完整中间产物

```text
outputs/bank1_full_after_no_w2w_20260811/
```

内容包括：

- `train/`：Bank1 Train 1159 题，构建错误为 0
- `judge/`：C=609/W=550，无永久 Judge 错误
- `diagnosis/`：Answer 213、Access 98、Construction 328 个 packages
- `success_examples.jsonl`：588 条 Judge-C Skill 使用样例
- `success_package_v2.jsonl`：504 条无 Skill 正确轨迹，含回答路径和引用记忆构建过程
- `candidate_shards/access/`：219 OK、83 NO_CHANGE、1 次生成错误
- `candidate_shards/construction/`：190 OK、85 NO_CHANGE
- `candidate_shards/success/`：21 OK、117 NO_CHANGE
- `skills_full_merged/candidates/`：最终合并 240 Access + 190 Construction Candidate
- `bank2_full_from_bank1/`：完整聚类、CRUD、版本链和发布 Bank2
- `full_val_judge/`、`full_test_judge/`：最终评测

这套产物足够做 Candidate 质量分析、Skill 增量归因和 Bank1→Bank2 回归分析，不要重跑前半段。

### 4.5 代码状态

当前代码和文档已提交并推送：

```text
e47f3b6 完善诊断学习流程并保留Bank1实验链路
```

提交前测试：`91 passed`。

注意：`outputs/` 被 `.gitignore` 忽略。上述运行产物只在当前机器本地存在，不会随 Git clone 下载；Git 只保存代码、配置、Prompt、测试和报告。

### 4.6 代码文件导航

| 功能 | 主要文件 |
|---|---|
| Runtime 总编排、首次默认搜索后再取 Skill | `src/mim/workflows/use.py` |
| Access ReAct、证据约束和 Skill 注入 | `src/mim/agents/access.py`、`prompts/access.md` |
| Construction 单次提取与确定性 ADD/SKIP | `src/mim/agents/construction.py`、`prompts/construction_extraction.md`；`construction_decision.md` 仅保留配置兼容 |
| Published Bank 加载、混合召回和 applicability reranker | `src/mim/skills.py` |
| 标准 Answer/Access/Construction 诊断 | `src/mim/agents/{answer_failure,access_failure,cons_failure}.py`、`prompts/diagnosis/` |
| C→W/W→C 和 W→W 诊断 | `src/mim/agents/flip_failure.py`、`scripts/run_flip_diagnosis.py` |
| 迭代案例构建 | `scripts/build_contrastive_pairs.py` |
| 成功包 v2 | `scripts/build_success_package_v2.py` |
| Candidate 生成 | `src/mim/agents/skill_learning.py`、`scripts/run_candidates_from_diagnosis.py`、`prompts/skill_maker/candidate_generation_*.md` |
| Candidate/CRUD schema | `src/mim/skill_maker/models.py` |
| Candidate 校验 | `src/mim/skill_maker/validator.py` |
| 聚类、CRUD 和发布 | `scripts/run_skill_bank_pipeline_v2.py` |
| 逐案例直接 CRUD | `scripts/run_direct_contrastive_crud.py`、`prompts/skill_maker/direct_case_crud.md` |
| 全量编排 | `scripts/run_full_iter_b0.py` |
| 并行 Val/Test | `scripts/run_parallel_eval.py` |
| Binary Judge | `scripts/judge_binary.py`、`prompts/judge/locomo_binary_judge.md` |

### 4.7 已清理的旧产物

为避免继续误用旧 Bank，已经删除 56 个无关或重复的本地输出目录，包括早期 `v2*`、`v3*`、`swap_*`、`val_clean_*`、`rebuild_iter*`、`bank2_obj`、`bank3_obj` 和 `b0_full_iter` 等。

因此旧文档如果引用这些路径，只能作为历史说明，不能按原路径继续运行。保留的有效目录以本文第 3、4 节为准。

---

## 5. 不需要重复做的工作

1. **不要重新生成当前 Bank1。** 它的 published Bank、source Candidate、聚类、草稿和事务链都在 `b1_joint_full_iter_high/`。
2. **不要因为怀疑目录错误而重跑 Bank1 全量前半段。** 已验证后续 Bank2 工作区的 `bank_v000` 与 Bank1 的 Access 65 条、Construction 34 条逐字段完全一致；Train 摘要也指向同一 published Bank 路径。
3. **不要重跑空 Bank 标准诊断。** 完整 Baseline Train、Judge 和三类诊断包已保留。
4. **不要重新生成无 W→W 的正反 Candidate。** 196 个文件已保存在 `bank_new_20260811/skills/candidates/`，并已完成 CRUD 与评测。
5. **不要重跑 Bank1 全量诊断和 Candidate 生成。** Answer/Access/Construction 包、成功包 v2、三个 Candidate shard 和合并 Candidate 都已存在。
6. **不要继续把 Bank_NEW 或 Bank2 full 当作新基座。** 两者 Val/Test 都未超过 Bank1。
7. **不要用 Val/Test 翻转生成 Skill。** 所有学习必须只来自 Train；Val/Test 只用于最终验证。
8. **不要从 C→C 生成差分 Candidate。** C→C 只作为稳定成功统计。
9. **不要从纯成功案例生成 Construction Skill。** 当前设计只允许从非平凡成功路径学习 Access Skill；构建过程只是因果背景和默认策略保护证据。
10. **不要把 Skill 写成长诊断报告。** Runtime Skill 只保留窄触发和简短动作，所有 case-specific 溯源放 Candidate 元数据。

---

## 6. 容易踩坑的点

### 6.1 Bank 路径和版本号

- 不要根据目录名猜 Bank；启动前读取 pipeline `summary.json` 和 published 文件名。
- 指定 full Bank 时使用 `published_bankN_full`，里面应同时存在 `access_skill_bank_vN.json` 和 `construction_skill_bank_vN.json`。
- `published_bank2`、`published_bank2_full`、`access_only`、`construction_only` 是不同评测对象。
- 本轮已验证 Bank1→Bank2 起点没有用错目录。

### 6.2 不同 Baseline 不可混用

`outputs/b1_joint_full_iter_high/baseline/` 是当前 Bank1 的配套 Baseline（54.85/53.10）。

`outputs/baseline_empty_20260810/` 是另一次独立运行（51.79/51.72），仅作为历史产物保留。旧报告中的 53.3/51.3 也来自更早批次。不同时间、Runtime 状态或代码版本的 Baseline 不能直接做百分点差。

### 6.3 评测没有完全确定性

- Judge 已关闭 thinking 并设 temperature=0，当前摘要均无永久 Judge 错误。
- Runtime 仍可能因模型服务和生成路径产生小幅波动；同一 Bank1 Train 两次为 615C 和 609C。
- 所以 Bank1 与新 Bank 的严格比较应在同一代码、同一配置、尽量同一时间重新成对运行，并按 `qa_id` 做 C→W/W→C 分析。

### 6.4 输出文件只在整个对话完成后汇总

`scripts/run_parallel_eval.py` 会先写每个 `run_id_conv-*` 的 SQLite 和 trace，等全部 conversation 完成后才在总目录写 `qa_results.jsonl` 和 `summary.json`。中途看不到总 QA 文件不代表卡死；应查看：

```text
outputs/<run_id>_conv-XX/traces/access_traces.jsonl
outputs/<run_id>_conv-XX/state/memory.sqlite3
```

Val/Test 各自完整运行通常需要约 50–60 分钟。

### 6.5 Resume 和重复数据

- `judge_binary.py` 的输出目录已存在时必须使用 `--resume`，否则会立即退出。
- Train/Judge 遇到少量网络错误或 Windows `[Errno 22] Invalid argument` 时，先确认已有进度，再 `--resume`；不要整轮重跑。
- 旧 runner 可能写 `locomo_predictions.jsonl`，新并行 runner 写 `qa_results.jsonl`。Judge 前确认输入文件、总题数和 `qa_id` 唯一性。
- 正确总数：Train 1159、Val 392、Test 435。

### 6.6 并发与输出目录

- Candidate 生成可分 Access、Construction、Success 三个 shard 并行；完成后再合并。
- CRUD 针对同一个 shadow Bank 的逐事务操作必须保持有序；不能让多个进程同时写同一个 working Bank。
- `run_skill_bank_pipeline_v2.py --resume` 会复用已有草稿和事务，不要同时再启动第二个相同 run-id。
- PowerShell `Start-Process` 参数容易因路径和重定向引号出错；启动后立刻检查完整 `CommandLine` 和输出目录更新时间。

### 6.7 `outputs/` 不在 Git 中

Git 已保存代码，但没有保存 2GB 以上的运行产物。不要误以为 push 后其他机器自动拥有 Bank1。迁移机器时必须单独复制保留的 `outputs` 白名单，至少复制 Bank1、Baseline 诊断和当前衍生实验目录。

---

## 7. 当前主要问题

### P0：Bank 扩张导致 Skill 检索干扰

Bank1 有 99 条 Skill；Bank_NEW 增至 123 条；Bank2 full 增至 149 条。Bank2 full 没有删除 Bank1 原 Skill，只修改了 4 条原 Skill并新增 50 条：

| 侧 | Bank1 | Bank2 full | 原 Skill 修改 | 新增 | 删除 |
|---|---:|---:|---:|---:|---:|
| Access | 65 | 89 | 3 | 24 | 0 |
| Construction | 34 | 60 | 1 | 26 | 0 |

Runtime 每侧最终最多选择 1 个 Skill。大量相似的新 Skill 会和原来有效的 Skill 竞争候选和 reranker 位置，因此即使没有删除旧 Skill，也可能系统性回归。当前最可疑的问题是**Skill 池膨胀与错误触发**，而不是起始 Bank 用错。

### P0：缺少发布前的质量门

当前流程可以把几百个 Candidate 聚类后一次性 CRUD，pipeline 摘要中的 `rejected_source_candidates` 为 0。Candidate generator 虽然会输出 NO_CHANGE，但进入 pipeline 后缺少按真实行为收益进行的 promotion gate。

需要建立至少一种 Train 内部质量门：

- 按 conversation 留一或固定 Train calibration 子集；
- 对每个 Candidate/cluster 做 shadow replay；
- 新增 Skill 必须修复目标 W→C，且不得制造超过阈值的 C→W；
- 优先 REVISE/MERGE/PRESERVE，限制一次迭代的净新增数量；
- 只有通过门槛的 shadow Bank 才发布 Val/Test。

### P0：Bank1 的 56.63/54.48 是历史运行，不是与最新 Bank2 同时重跑

Bank lineage 已确认正确，但最新 Bank2 结果与 Bank1 的历史结果跨时间比较。下一步首先应在当前代码/配置下重新跑一次原 Bank1 Val/Test，再与现有 Bank2 或同步重跑的 Bank2 做题级对比。否则无法完全区分 Skill 回归与 Runtime 波动。

### P1：Construction Skill 的副作用范围过大

Construction Skill 对整段 session 生效。一个看似合理的“保留更多细节”规则会改变整段对话的候选数量、合并频率和时间字段，进而影响几十到几百道后续 QA。Construction Candidate 必须比 Access Candidate 更保守，并强制包含不适用边界。

### P1：W→W 诊断机制已实现，但尚未证明带来净增益

当前代码支持 W→W、`failure_age`、prior/current wrong run 和 repair lineage。Three-iter 实验包含 W→W 后仍未超过 Bank1；排除 W→W 的 Bank_NEW 也退化。因此问题不是简单的“保留或删除 W→W”，而是 Candidate 的选择、合并和发布门槛。

### P1：正样本机制已经跑通，但第一轮收益不稳定

纯成功包 v2 已包含完整回答路径和记忆构建 provenance；正样本 generator 也只产生非平凡 Access Candidate。最近全量迭代中 138 个非平凡成功包只产生 21 个 Candidate，大部分正确返回 NO_CHANGE，这一方向符合“保持 Skill 简洁”的目标。但这些 21 个 Candidate 与失败 Candidate 混合发布后仍未带来净提升，需要做增量消融，而不是继续扩大成功包数量。

### P2：现有指标样本量小，单对话差异很大

Validation 只有两个 conversation，Bank1 的整体提升主要来自 conv-41（+6.22pp），conv-26 反而下降。Test 同样只有 conv-48/49。应始终报告逐对话结果和题级翻转，不只看总体百分比。

---

## 8. 推荐的下一步

按优先级执行：

1. **当前环境重跑原 Bank1 的 Val/Test + Judge。** 使用同一个 `qwen3_8b_swap_b0.yaml`，不要生成任何新 Skill。
2. **与 Bank2 full 做题级配对。** 统计 C→W/W→C/W→W/C→C，并检查每个 C→W 的首轮检索结果、最终选中 Skill 和附近未选 Skill。
3. **验证“池膨胀”假设。** 分别评测 Bank2 full 的 Access-only 增量、Construction-only 增量，或只保留修改原 Skill、不添加新 Skill 的消融 Bank。
4. **实现 Train 内 promotion gate。** 在进一步迭代前限制净新增 Skill，按 Candidate/cluster 的目标修复与回归数决定 ADD/REVISE/REMOVE/PRESERVE。
5. **门槛通过后再恢复正式循环。** 正式循环仍是：Train 评测 → C→W/W→C/W→W → 经验包 → Candidate + CRUD → 发布 → Val/Test。

在第 1–3 步完成前，不建议从 Bank2 或 Bank_NEW 继续向下迭代。

---

## 9. 常用命令

在 `single_agent_mim/` 下运行。

### 9.1 直接评测一个 published Bank

```powershell
python -u scripts/run_parallel_eval.py --config configs/qwen3_8b_swap_b0.yaml --split-name validation --mode mim --skill-bank-dir outputs/b1_joint_full_iter_high/bank/bank1_joint/skills/published_bank1_full --run-id <val_run_id>
python -u scripts/run_parallel_eval.py --config configs/qwen3_8b_swap_b0.yaml --split-name test --mode mim --skill-bank-dir outputs/b1_joint_full_iter_high/bank/bank1_joint/skills/published_bank1_full --run-id <test_run_id>
```

### 9.2 Judge

```powershell
python -u scripts/judge_binary.py --config configs/qwen3_8b_swap_b0.yaml --workers 12 --output-dir outputs/<val_judge> outputs/<val_run_id>/qa_results.jsonl
python -u scripts/judge_binary.py --config configs/qwen3_8b_swap_b0.yaml --workers 12 --output-dir outputs/<test_judge> outputs/<test_run_id>/qa_results.jsonl
```

输出目录存在时增加 `--resume`。

### 9.3 从已有 Bank 做全量迭代

```powershell
python -u scripts/run_full_iter_b0.py --config configs/qwen3_8b_swap_b0.yaml --output-root outputs/<new_run> --initial-bank outputs/b1_joint_full_iter_high/bank/bank1_joint/skills/published_bank1_full --run-id <bank_run_id> --baseline-root outputs/empty_bank_two_phase_full_v3_20260809
```

已有 Train 时可使用 `--skip-train`；只想发布、不做 held-out 评测时可使用 `--skip-eval`。

### 9.4 从诊断包生成分片 Candidate

```powershell
python -u scripts/run_candidates_from_diagnosis.py --config configs/qwen3_8b_swap_b0.yaml --diagnosis-root <diagnosis_root> --skills-dir <access_shard> --package-source failure --failure-side access --workers 12 --max-concurrency 12
python -u scripts/run_candidates_from_diagnosis.py --config configs/qwen3_8b_swap_b0.yaml --diagnosis-root <diagnosis_root> --skills-dir <construction_shard> --package-source failure --failure-side construction --workers 12 --max-concurrency 12
python -u scripts/run_candidates_from_diagnosis.py --config configs/qwen3_8b_swap_b0.yaml --diagnosis-root <diagnosis_root> --skills-dir <success_shard> --package-source success --success-package <success_package_v2.jsonl> --workers 12 --max-concurrency 12
```

### 9.5 Candidate → Bank 发布

```powershell
python -u scripts/run_skill_bank_pipeline_v2.py --config configs/qwen3_8b_swap_b0.yaml --source-candidates <merged_candidates> --run-id <bank_run_id> --output-root outputs/<new_run> --initial-skill-bank-dir outputs/b1_joint_full_iter_high/bank/bank1_joint/skills/published_bank1_full --workers 6
```

中断后只能在确认没有同名活动进程的情况下使用 `--resume`。

---

## 10. 最后检查清单

开始新实验前确认：

- [ ] 起始 Bank 的绝对路径和 pipeline summary 一致
- [ ] published Bank 同时有 Access/Construction 文件且版本一致
- [ ] 学习数据只来自 Train
- [ ] Train/Val/Test 总题数分别为 1159/392/435
- [ ] construction errors 为 0
- [ ] Judge `permanent_errors[0]` 为 0
- [ ] `qa_id` 无重复、无缺失
- [ ] Candidate generator 的 error 已 retry 或明确记录
- [ ] 同一个 working Bank 没有多个 CRUD 进程并发写入
- [ ] 新版本先和当前环境重跑的 Bank1 比较，再决定是否成为新基座
- [ ] 报告总体与逐 conversation 结果

当前最重要的原则不是继续堆叠更多 Skill，而是建立一个能阻止错误 Skill 进入正式 Bank 的验证门。

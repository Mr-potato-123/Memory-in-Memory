# MiM 实验交接报告(项目介绍、流程与问题全景)

> 生成日期:2026-08-09 | 状态:系统设计变更讨论中,实验暂停 | 接手人:后续 Claude 会话/协作者

---

## 0. 项目介绍

### 0.1 一句话定位

**MiM(Memory in Memory)= 错误驱动的元记忆层(memory layer)**:在下游 LLM 长对话记忆问答出错时,系统自动诊断错误根源(记忆构建侧 vs 记忆访问侧),把修复经验抽象为**可复用的自然语言 Skill**,注入运行时以改进后续行为。不预先定义"什么是好记忆",而是**从错误中学习**。

### 0.2 系统双体架构

| 角色 | 模型 | 职责 |
|---|---|---|
| **运行时(runtime)** | qwen3-8b(DashScope) | 干活:记忆构建(提取+CRUD)+ 访问答题(ReAct 检索) |
| **维护侧(maintenance)** | deepseek-v4-flash(3 key 轮换) | 学习:judge、三阶段诊断、翻转诊断、候选生成、CRUD 规划 |

运行时只加载**物理隔离的 published Bank**(access + construction 各一个文件,版本号一致);维护侧通过原子事务发布新版本,skill 带 `parent_version_id` 和 `created_from_failure_id` 溯源链。

### 0.3 核心概念

- **Skill**:三字段 `name` + `description`(触发条件)+ `content[]`(指令),分 access/construction 两侧。运行时检索 = 混合检索(0.7 语义 + 0.3 BM25,只匹配 name+description)+ LLM 适用性路由;注入哲学是 **"advisory references, not commands"**(证据优先,默认策略优先)。
- **记忆**:SQLite 存储,版本化(memory_versions + 版本链),构建侧两阶段(提取候选 → 批量 CRUD 决策),访问侧 ReAct 循环(search 6 种策略 + inspect + answer,证据验证 + 预算兜底)。
- **诊断**:错误 → 三阶段(answer→access→cons)定位根源,产出 repair_package;近年新增**翻转配对诊断**(双侧对比)。
- **成功经验包**:无 skill 也能答对的 C 题索引,用于校准候选生成——"默认策略已足够的问题模式,skill 不得破坏"。
- **Skill Bank 迭代**:train 上学习(诊断→候选→聚类→CRUD)→ 发布新档 → val/test 客观评测。

### 0.4 评测

- 基准:**LoCoMo**(locomo10.json),swap split:train=conv-30/42/43/44/47/50、val=conv-26/41、test=conv-48/49
- 判官:deepseek-v4-flash **binary C/W**(确定性:thinking 关闭,temperature=0 才生效)
- 对比纪律:同批同判官;judge 前必须去重;对比前校验 commits 完整性

### 0.5 关键里程碑

| 档 | 方式 | val | test |
|---|---|---|---|
| baseline | 无 skill | 53.3% | 51.3% |
| bank1_rebuild | 空 bank+成功经验包(客观) | 50.0% | 56.3% |
| **bank3_obj** | **第2轮 train 翻转(客观,当前最佳)** | **54.3%** | **55.6%** |

### 0.6 代码/产物规模

- 主工程 `single_agent_mim/`:agents/、diagnosis/、skill_maker/、scripts/(~20 个运行脚本)、prompts/(runtime + 诊断 + 候选生成)
- 产物:6+ 个 bank 档、4 轮迭代评测、成功经验包 600+ 案例

---

## 1. 当前流程(两套迭代机制)

### 1.1 标准全量迭代(run_full_iter_swap.py)
```
train 6 对话构建+答题(当前 bank,6 路并行)
  → judge(binary C/W,确定性)
  → 三阶段诊断(answer → access+cons,只诊断 W 题)
  → 成功案例(build_successful_skill_traces.py,有 skill 且答对的轨迹)
  → 成功经验包(build_success_package.py,无 skill 答对的 C 题,432+ 案例)
  → 候选生成(run_candidates_from_diagnosis.py,6 路并行,带两类校准)
  → CRUD(run_skill_bank_pipeline_v2.py,聚类→草稿→发布)
  → val + test 构建+judge → 评测报告
```

### 1.2 翻转配对诊断迭代(run_flip_diagnosis.py)
```
翻转对(两个 bank 版本在相同对话上的 label 差异,双向 C→W/W→C)
  → 答错侧标准三阶段诊断(给翻转题)
  → 配对诊断:答对侧(skill_trace+召回记忆+检索动作)vs 答错侧(标准包)
    FlipDiagnosisAgent → 归因 access/construction → repair_package
  → 候选生成(带成功经验包校准)
  → CRUD(seed 可选)→ 发布 → val+test 评测
```

### 1.3 用户要求的正式迭代序列(尚未按此完整执行)
```
第1轮: 全量 + 翻转合并(候选合并 CRUD)
第2轮: 全量
第3轮: 翻转
```

## 3. 诊断包结构

### 3.1 标准三阶段包
| 阶段 | 问的问题 | 输出要点 |
|---|---|---|
| answer | 答案能否从已检索上下文直接得出? | claims(essential_reference_claims + supporting_version_ids + coverage) |
| access | 访问侧是否检索到足够证据? | missing_useful_current_version_ids(代码算集合差) |
| cons | 构建侧是否提取/保留了该事实? | primary_subtype、construction_skill_traces、版本链(渐进披露) |

### 3.2 翻转包(flip_diagnosis_v1)
```
{
  flip: {chain, direction, from, to},
  diagnosis_type: ACCESS_FAILURE|CONS_FAILURE|NO_CHANGE,
  side, reason, confidence,
  repair_package: {problem_description, correct_side_behavior,
                   wrong_side_behavior, skill_guidance[]},
  skill_trace: 答错侧 access 轨迹
}
```

## 4. 评测结果全景(swap split,同判官同批)

| 档 | 方式 | 规模 | val | test |
|---|---|---|---|---|
| baseline | 无 skill | 0 | 53.3% | 51.3% |
| v1_b | 旧空 bank 诊断 | 118 | 53.3% | 52.9% |
| v2_b ⚠️ | v1_b 迭代(膨胀) | 171 | 46.7% | 56.8% |
| v2_c ⚠️ | val 翻转修复(泄漏) | 133 | 54.3% | 52.9% |
| bank4 | v2_c 全量(客观) | 161 | 53.6% | 52.6% |
| bank5 ⚠️ | val/test 翻转(泄漏) | 178 | 55.6% | 54.7% |
| bank1_rebuild | 空 bank+成功包(客观) | 61 | 50.0% | 56.3% |
| bank2_rebuild ⚠️ | val/test 翻转(泄漏) | 81 | 53.8% | 56.1% |
| bank2_obj | 第1轮 train 翻转(客观) | 77 | 51.0% | 52.9% |
| **bank3_obj** | **第2轮 train 翻转(客观)** | **90** | **54.3%** | **55.6%** |

⚠️ = 基于 val/test 翻转生成,评测泄漏,结论不可信。

## 5. 当前面临的问题(按严重度)

### P0:方法学泄漏(已证实,已修正方向)
- v2_c/bank5/bank2_rebuild 用 val/test 翻转生成 skill 再在 val/test 评测 = 在评测集上训练
- 泄漏效应量化:约 +2.8pp val / +3.2pp test
- **已修正:翻转必须基于 train**(train 上学习,val/test 验证)

### P0:迭代序列执行偏差
- 用户要求:第1轮全量+翻转合并 → 第2轮全量 → 第3轮翻转
- 实际执行:bank1(全量)→ bank2_obj(翻转)→ bank3_obj(翻转),第1轮缺全量合并,连续两轮翻转
- 修正方案:run_alternating_iter.py 自动编排(未写)

### P1:翻转包 cons 判定缺 skill 归因
- 翻转包输入只有访问产物,看不到构建侧 skill_trace
- 判了 CONS_FAILURE 却不知道是哪个 construction skill 干的(对比:标准 cons 包有 construction_skill_traces 能归因)
- 修复方案(模块 A):build_flip_case 增加 construction_context(construction_skill_traces + affected_memory_history),提示词要求 CONS 判定必须归因到具体 skill_id

### P1:双侧对比靠 LLM 猜,无确定性锚点
- 标准包有代码计算的集合差(missing_useful_current_version_ids),翻转包没有
- 修复方案(模块 B):代码算 evidence_diff = 答对侧可见 − 答错侧可见;claim 级分解(essential_reference_claims + 双侧 supporting + coverage)

### P2:合并时旧轨迹污染
- 58 个翻转候选带 related_existing_skill_ids 指向旧 bank
- merge_candidates.py 已写(全部清空),建议升级为按 seed bank 白名单过滤(保留指向 seed 的 id 以便 CRUD 合并到已有)

### P2:小模型 CRUD 方差
- qwen3-8b 对提示词扰动敏感,同会话同候选不同轮次判 ADD/MERGE 不同
- 版本链放大:一次错误合并逐会话累积污染(conv-26 mem_0030 案例)

### P3:训练构建偶发 QA error
- 每轮 train 有 2-5 个 Connection error,需清理 error 行 + resume 补答 + 重 judge(流程已固化)

## 6. 待用户决策的事项

1. **实施顺序**:先改诊断(A+B)再跑,还是先验证编排再改?(用户暂未定,要求先出本交接报告)
2. **merge_candidates 清洗策略**:全部清空 vs seed 白名单过滤
3. **第3轮之后是否继续交替**(第4轮全量、第5轮翻转...)
4. 泄漏档(v2_c/bank5/bank2_rebuild)结论是否在文档中标注作废

## 7. 关键脚本/产物索引

| 组件 | 路径 |
|---|---|
| 标准全量编排 | `scripts/run_full_iter_swap.py` |
| 翻转配对诊断 | `scripts/run_flip_diagnosis.py` + `src/mim/agents/flip_failure.py` + `prompts/diagnosis/flip_diagnosis.md` |
| 成功经验包 | `scripts/build_success_package.py` + `src/mim/skill_maker/success_examples.py`(NoSkillSuccessIndex) |
| 候选合并清洗 | `scripts/merge_candidates.py`(未验证) |
| 候选生成 | `scripts/run_candidates_from_diagnosis.py`(--success-examples/--success-package/--max-concurrency 默认6) |
| CRUD 发布 | `scripts/run_skill_bank_pipeline_v2.py`(--resume 已支持) |
| 客观档产物 | bank3_obj: `outputs/bank3_obj/bank3/skills/published_bank4_full` |
| train 翻转输入 | `outputs/flip_diagnosis_input_train_b{1,2}.json` |
| 未完成:第1轮全量候选 | `outputs/bank1_full_diag/`(诊断完成,候选未生成) |
| 未完成:翻转候选清洗 | `outputs/flip_train_b1_skills/candidates/`(147 access 候选待合并) |

## 8. 环境注意

- 运行时 API 依赖外网,断网时全部阻塞(曾中断 resume)
- 每轮 train 需检查 commits=sessions、清理 error 行、judge 前去重
- 并发:构建 6 路、诊断 4 路、候选 6 路、judge 8 路
- bank 发布目录名:seed bank 版本号会进位(published_bankN_full),启动前 ls 确认

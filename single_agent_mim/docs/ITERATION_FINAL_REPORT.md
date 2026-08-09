# MiM 迭代实验总报告(2026-08-08)

> 判定:deepseek-v4-flash binary C/W(确定性,同 prompt)| runtime:qwen3-8b | swap split | 同批对比

## 1. 版本全景

| 版本 | 迭代方式 | 规模 | val | conv-26 | conv-41 | test | conv-48 | conv-49 |
|---|---|---|---|---|---|---|---|---|
| baseline | 无 skill | — | 53.3%* | — | — | 51.3% | — | — |
| v1_b | 空 bank 纯诊断 | 27A+91C=118 | 53.3% | 51.3% | 55.4% | 52.9% | — | — |
| v2_b | v1_b 迭代(膨胀) | 43A+128C=171 | 46.7% | 37.2% | 56.5% | 56.8% | — | — |
| **v2_c** | **v1_b 回滚+翻转修复** | 38A+95C=133 | 54.3% | 52.3% | 56.5% | 52.9% | 55.6% | 50.0% |
| bank4 | v2_c 全量迭代 | 48A+113C=161 | 53.6% | 50.8% | 56.5% | 52.6% | 59.0% | 44.9% |
| **bank5** | **bank4 正反例迭代** | 60A+118C=178 | **55.6%** | 50.8% | **60.6%** | **54.7%** | 55.6% | **53.6%** |

*baseline 为 swap_val_base 同批值。

## 2. 方法论收获(排序按重要性)

1. **翻转配对诊断 > 标准全量诊断**:同样+~17 条 skill,配对诊断(val +1.3pp / test +2.1pp vs bank4)显著优于标准全量(val −0.7pp vs v2_c)。**答对侧的 skill_trace + 召回记忆是更有效的学习信号**——它告诉模型"什么行为不能破坏"。
2. **新增 skill 的代价是 OOD 对话**:三次迭代(bank4/v2_b)新增 skill 都在 conv-26(全新人物)上产生负效应;配对诊断能识别并修复这种伤害(bank5 把 conv-49 从 44.9% 拉回 53.6%)。
3. **规模控制有效**:v2_c(+15)、bank5(+17)的小步 CRUD 优于 v2_b 的 +53 膨胀。
4. **数据卫生是评测的前提**:v2b_val 的 10 条重复行直接扭曲对比(v2_b 名义 56.5% 与真实 46.7% 的差距);judge 前必须去重。
5. **回滚路线有效**:v2_b 失败后从 v1_b 重发 + 翻转修复,v2_c 一举超过 v1_b。

## 3. 产物清单

| 产物 | 路径 |
|---|---|
| v2_c bank | `outputs/v2c_iter/v2c/skills/published_bank3_full` |
| v2_c 评测 | `outputs/v2c_eval/{val,test}/judge/summary.json` |
| bank4 | `outputs/v2c_full_iter/bank4/skills/published_bank4_full` |
| bank5 | `outputs/bank5_iter/bank5/skills/published_bank5_full` |
| bank5 评测 | `outputs/bank5_eval/{val,test}/judge/summary.json` |
| 配对诊断工具 | `scripts/run_flip_diagnosis.py` + `src/mim/agents/flip_failure.py` + `prompts/diagnosis/flip_diagnosis.md` |
| 全量迭代编排 | `scripts/run_full_iter_swap.py` |
| 报告 | `docs/V2C_ITERATION_REPORT.md`、`docs/BANK4_ITERATION_REPORT.md`、`docs/BANK5_ITERATION_REPORT.md` |

## 4. 遗留问题

1. **conv-26(全新人物)仍是最顽固的 OOD 难点**:各档均在 50.8-52.3% 徘徊,低于 conv-41 的 56-60%。skill 触发与主题耦合问题未根本解决。
2. **v2_b 的 test 增益(56.8%)未被继承**:翻转诊断将其裁掉(判定为 conv-26 破坏源),换取了两集平衡。
3. 配对诊断的 60 个"无问题"判定中可能仍有可修复样本——可调低问题判定门槛再试。
4. 构建侧守卫(事件合并禁止、world_start 保护)仍未实现为代码级防御,当前靠 skill 提示词约束。

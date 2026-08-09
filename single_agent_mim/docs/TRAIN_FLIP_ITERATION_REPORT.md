# 客观正反例迭代报告(train 翻转,两轮)

> 生成日期:2026-08-09 | 判定:deepseek-v4-flash binary C/W(确定性)| runtime:qwen3-8b | swap split

## 0. 方法学修正(重要)

**此前的配对诊断迭代(v2_c/bank5/bank2_rebuild)基于 val/test 翻转生成 skill 再在 val/test 评测 = 评测集泄漏**。本报告全部基于 **train 翻转**(在 train 上学习,val/test 上验证)。

## 1. 迭代链(全部客观)

```
bank_empty → bank1_rebuild(61 条,标准诊断+成功包)
  → 第1轮:bank1_rebuild 在 train 跑(C=619/W=540)
     train 翻转(空 bank↔bank1,260 题)→ 配对诊断(251 包)→ 候选 203 → bank2_obj(77 条)
     → val 51.0% / test 52.9% (无增益)
  → 第2轮:bank2_obj 在 train 跑(C=611/W=548)
     train 翻转(bank1↔bank2_obj,248 题)→ 配对诊断(237 包)→ 候选 159 → bank3_obj(90 条)
     → val 54.3% / test 55.6% (+3.3/+2.7pp)
```

## 2. 评测结果(swap split,全部同批同判官)

| 档 | 方式 | 规模 | val | conv-26 | conv-41 | test | conv-48 | conv-49 |
|---|---|---|---|---|---|---|---|---|
| baseline | 无 skill | 0 | 53.3% | 48.7% | 58.0% | 51.3% | 54.4% | 47.4% |
| bank1_rebuild | 标准诊断+成功包 | 61 | 50.0% | 49.2% | 50.8% | 56.3% | 56.5% | 56.1% |
| bank2_obj | 第1轮 train 翻转 | 77 | 51.0% | 47.7% | 54.4% | 52.9% | 54.4% | 51.0% |
| **bank3_obj** | **第2轮 train 翻转** | **90** | **54.3%** | **50.3%** | **58.5%** | **55.6%** | 59.0% | 51.5% |
| ~~bank2_rebuild~~ | ~~val/test 翻转(泄漏)~~ | 81 | ~~53.8%~~ | — | — | ~~56.1%~~ | — | — |
| ~~bank5~~ | ~~val/test 翻转(泄漏)~~ | 178 | ~~55.6%~~ | — | — | ~~54.7%~~ | — | — |

## 3. 关键结论

1. **train 翻转迭代有效,但需要多轮**:第1轮与 bank1 原有 train 诊断(550 W 题)信息重叠,无增益;第2轮 +3.3pp val / +2.7pp test。**单轮评估会误判方法无效**
2. **泄漏效应真实存在**:val/test 翻转迭代第一轮虚高约 +2.8pp val / +3.2pp test
3. **客观档最终与泄漏档持平甚至更优**:bank3_obj(val 54.3% / test 55.6%)vs bank2_rebuild(53.8/56.1)——train 翻转是合法且有效的替代
4. **bank3_obj = 当前最佳客观档**:90 条 skill(val+test 合计 55.0%),规模仅为泄漏档 bank5(178 条)的一半
5. conv-26 首次突破 50%(50.3%)——前几轮一直卡在 47-49%

## 4. 产物

| 产物 | 路径 |
|---|---|
| bank1_rebuild train | `outputs/bank1_train/`(judge: C=619/W=540) |
| bank2_obj | `outputs/bank2_obj/bank2/skills/published_bank3_full` |
| bank3_obj | `outputs/bank3_obj/bank3/skills/published_bank4_full` |
| 评测 | `outputs/bank{2,3}_obj/eval/{val,test}/judge/summary.json` |
| train 翻转输入 | `outputs/flip_diagnosis_input_train_b{1,2}.json` |

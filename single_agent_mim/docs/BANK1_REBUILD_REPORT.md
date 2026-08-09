# bank1_rebuild(空 bank 从 0 重建 + 成功经验包)评测报告

> 生成日期:2026-08-08 | 判定:deepseek-v4-flash binary C/W(确定性)| runtime:qwen3-8b | swap split

## 1. 本轮改动(成功经验包机制)

1. **提示词强化"默认策略优先"**:
   - runtime 注入([access.py](single_agent_mim/src/mim/agents/access.py)/[construction.py](single_agent_mim/src/mim/agents/construction.py)):default-first——默认策略是已验证基线,简单直查永不因 skill 改变策略
   - 候选生成([candidate_generation_access/construction.md](single_agent_mim/prompts/skill_maker/)):`DEFAULT_POLICY_SUCCESS_EXAMPLE` 校准——匹配到无 skill 成功案例 → 倾向 NO_CHANGE 或"仅当默认策略失败时触发"的窄 skill
2. **成功经验包常态化**([build_success_package.py](single_agent_mim/scripts/build_success_package.py) + [NoSkillSuccessIndex](single_agent_mim/src/mim/skill_maker/success_examples.py)):
   - 从空 bank baseline(val+test 的 432 个 C 题)+ 本轮 train 无 skill C 题(177 个)→ **609 条成功经验**
   - 候选生成自动附加最相似案例作为校准(默认 6 路并行)

## 2. 迭代链

```
bank_empty(空 bank, 新提示词)
  → swap train 6 路(0 构建错误)→ judge(1159 题)
  → 三阶段诊断(550 W 题)→ 成功经验包(609 条)
  → 候选生成(带 DEFAULT_POLICY 校准)→ CRUD → bank1_rebuild
  → val + test 评测
```

**构建完整性:10/10 对话 commits=sessions,0 错误(无需重跑)**

## 3. 评测结果

| 版本 | val | conv-26 | conv-41 | test | conv-48 | conv-49 |
|---|---|---|---|---|---|---|
| baseline(空 bank) | 53.3% | 48.7% | 58.0% | 51.3% | 54.4% | 47.4% |
| v1_b(旧,无成功包) | 53.3% | 51.3% | 55.4% | 52.9% | — | — |
| **bank1_rebuild** | 50.0% | 49.2% | 50.8% | **56.3%** | 56.5% | 56.1% |
| bank5(现最优) | 55.6% | 50.8% | 60.6% | 54.7% | 55.6% | 53.6% |

### 关键结论

1. **test 达到历史最高 56.3%**(+5.0pp vs baseline,+3.4pp vs v1_b):conv-49 从 47.4% → 56.1%(+8.7pp)。成功经验包+新提示词的保守性在分布外对话上显著有效——skill 不再过度干预
2. **val 反而下降**(50.0%,-3.3pp vs baseline):conv-41 从 58.0% 崩到 50.8%(-7.2pp)。新提示词"默认优先"可能让模型在 conv-41 上搜索不足/更易弃答,或重建的 skill 在 val 上仍有害——**需要翻转分析归因**
3. 与 bank5 对比:bank1_rebuild test 更高(+1.6pp)但 val 低 5.6pp——两集平衡不如 bank5
4. 成功经验包机制本身跑通(609 案例、候选生成校准生效),价值方向正确(test 证明),但需要在 val 上做翻转修复迭代

## 4. 下一步建议

- 对 bank1_rebuild 的 val 翻转(vs baseline)做配对诊断迭代(同 bank4→bank5 模式)→ 修复 conv-41 的下降
- 或者:以 bank5 为当前最优档,把成功经验包机制融入 bank5 的下一轮迭代(比从 0 重建更稳)

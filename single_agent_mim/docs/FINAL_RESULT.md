# MiM 最终结果(定版)

> 2026-08-05 | validation conv-26/41,392 题 | judge:deepseek-v4-flash,community-standard binary CORRECT/WRONG(宽松语义判断)

## 版本定义

| 版本 | Skill Bank | 说明 |
|---|---|---|
| **v0** | 无 skill | 基线 |
| **v1** | 23A+26C | 提示版(advisory 注入)定版 |
| **v2** | 32A+49C | 由 v1 经 V2 迭代生成:诊断修复 + 保守性 skill 生成提示词 |

## 总体结果

| 指标 | v0 | v1 | **v2** |
|---|---|---|---|
| C | 197 | 206 | **227** |
| W | 192 | 184 | **165** |
| **C 率** | **50.3%** | **52.6%** | **57.9%** |

## 各题型 C 率

| 题型 | 题数 | v0 | v1 | **v2** |
|---|---|---|---|---|
| Multi-hop | 63 | 49% | 44% | 49% |
| Temporal | 64 | 34% | 48% | **56%** |
| Open-domain | 21 | 48% | 57% | 57% |
| Single-hop | 156 | 62% | 63% | **69%** |
| Adversarial | 88 | 43% | 41% | **47%** |
| **总 C 率** | 392 | 50.3% | 52.6% | **57.9%** |

## 结论

1. **v2(32A+49C)是当前最优**,总 C 率 57.9%,比 v1 +5.3pp
2. 归因实验确认:同批代码下 v2 比 v1 高 +6.1pp,全部来自新 skill(新 reranker 提示词单独跑 v1 仅 51.8%,无贡献)
3. 最大增益:Temporal +8pp、Single-hop +6pp;Adversarial +6pp(保守约束抑制了幻觉)
4. v1 在 Multi-hop 上曾低于 v0(44% vs 49%),v2 已修复回 49%

## 数据位置

- v0:exp/single-agent/bank_v0/validation/latest/
- v1:exp/single-agent/bank_v1/validation/latest_full/
- v2:exp/single-agent/bank_v2/validation/latest/
- 迭代过程:single_agent_mim/outputs/v2_loose_iter/(诊断包、candidates、bank3 构建过程)

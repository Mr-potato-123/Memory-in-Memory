# MiM 最终配置与结果(提示版)

> 生成日期:2026-08-04 | validation conv-26/41,392 题 | judge:deepseek-v4-flash(C/P/I 与 1-5 双方法)

## 1. 最终配置

| 组件 | 配置 |
|---|---|
| Skill Bank | 旧 Bank1(23A+26C,exp/single-agent/bank1_draft_crud_v2/banks) |
| 记忆构建 | 26C(独立构建,注入过往经验措辞) |
| 答题 Skill | 23A + 注入提示 |
| skill_candidate_k | 10(原始值) |
| reranker | Qwen3-8B(原始 prompt,未改动) |
| 注入措辞(access/construction) | **"advisory references, not commands"**(Skill 是参考策略非命令;简单题默认检索、复杂题可参考扩展、证据优先) |

### 代码改动清单

1. `src/mim/agents/access.py` `_build_system`:skill 渲染层加「advisory references, not commands」框架(简单题默认策略、复杂题可参考扩展、证据优先)
2. `src/mim/agents/construction.py` `_render_skills`:同款「advisory references, not mandatory commands」框架 + 保真底线
3. `src/mim/tracing.py`:写锁(并行答题线程安全)
4. `src/mim/skill_maker/validator.py`:content 条数限制 ≤8
5. `src/mim/skills.py` reranker prompt:**未改动**(保持原始)

## 2. 总体结果

| 指标 | 数值 |
|---|---|
| C / P / I | 177 / 67 / 148 |
| C 率(正确率) | 45.2% |
| C+P 率 | 62.2% |
| 1-5 总平均分 | 3.288 |
| 1-5 分数分布 | 5分:181 / 4分:25 / 3分:38 / 2分:22 / 1分:126 |

## 3. 各题型明细

| 题型 | 题数 | C | P | I | C 率(正确率) | C+P 率 | 1-5 平均 |
|---|---|---|---|---|---|---|---|
| Multi-hop | 63 | 24 | 27 | 12 | 38.1% | 81.0% | 3.460 |
| Temporal | 64 | 32 | 4 | 28 | 50.0% | 56.2% | 3.453 |
| Open-domain | 21 | 6 | 4 | 11 | 28.6% | 47.6% | 2.429 |
| Single-hop | 156 | 78 | 32 | 46 | 50.0% | 70.5% | 3.609 |
| Adversarial | 88 | 37 | 0 | 51 | 42.0% | 42.0% | 2.682 |

## 4. 对比基准

| 版本 | C+P | C 率 | 1-5 平均 |
|---|---|---|---|
| **提示版(最终)** | **244** | **45.2%** | **3.288** |
| 旧 Cons-only | 243 | 46.7% | 3.258 |
| 旧 Full | 242 | 44.4% | 3.253 |
| Bank0 基线 | 232 | 42.6% | 3.184 |


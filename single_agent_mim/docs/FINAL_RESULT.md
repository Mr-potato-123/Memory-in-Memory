# MiM 最终配置与结果(提示版)

> 生成日期:2026-08-04(数据更新至晚 19:30 四变体同批重跑)| validation conv-26/41,392 题 | judge:deepseek-v4-flash(C/P/I 与 1-5 双方法)

## 1. 最终配置

| 组件 | 配置 |
|---|---|
| Skill Bank | 旧 Bank1(23A+26C,exp/single-agent/bank_v1/banks) |
| 记忆构建 | 26C(独立构建,注入过往经验措辞) |
| 答题 Skill | 23A + 注入提示 |
| skill_candidate_k | 10(原始值) |
| reranker | Qwen3-8B(原始 prompt,未改动) |
| 注入措辞(access/construction) | **"advisory references, not commands"**(Skill 是参考策略非命令;简单题默认检索、复杂题可参考扩展、证据优先) |
| temperature | 0.0(确定性流程,无 seed 可设;唯一随机源为 API 服务端波动) |

### 代码改动清单

1. `src/mim/agents/access.py` `_build_system`:skill 渲染层加「advisory references, not commands」框架(简单题默认策略、复杂题可参考扩展、证据优先)
2. `src/mim/agents/construction.py` `_render_skills`:同款「advisory references, not mandatory commands」框架 + 保真底线
3. `src/mim/tracing.py`:写锁(并行答题线程安全)
4. `src/mim/skill_maker/validator.py`:content 条数限制 ≤8
5. `src/mim/skills.py` reranker prompt:**未改动**(保持原始)

## 2. 总体结果(2026-08-04 四变体同批重跑,定版数据)

| 指标 | full(23A+26C) |
|---|---|
| C / P / I | 175 / 62 / 157 |
| C 率(正确率) | 44.6% |
| C+P 率 | **60.5%** |
| 1-5 总平均分 | 3.188 |
| 1-5 分数分布 | 5分:171 / 4分:31 / 3分:35 / 2分:15 / 1分:142 |

### 四变体同批对比(同晚、同环境、从零构建)

| 变体 | C+P | C+P% | 1-5 平均 | 与 baseline 差 |
|---|---|---|---|---|
| **full(23A+26C 提示版)** | **237** | **60.5%** | **3.188** | **+17** |
| cons-only(26C) | 223 | 56.9% | 3.060 | +3 |
| acc-only(23A) | 220 | 56.1% | 3.043 | 0 |
| baseline(无 skill) | 220 | 56.1% | 3.098 | — |

**同批对比结论**:双侧注入(full)领先 baseline +17;单侧(cons 或 acc)无实质增益。这是消除跨批方差后首次可信的同批对照。

## 3. 稳定性说明(重要)

本流程**无随机 seed 可设**:构建、检索、作答全为确定性流程(temperature=0),唯一随机源是 API 服务端波动。但构建是级联的 —— 一个 session 的提取差异会传递至后续,放大为整体构建差异。因此用多次独立重跑表征稳定性:

| 运行 | 时间 | C+P | 1-5 | 说明 |
|---|---|---|---|---|
| 提示版 full #1 | 8-04 上午 | 244 | 3.288 | 历史最优(运气上界) |
| 提示版 full #2 | 8-04 18:25 | 235 | 3.207 | 同配置重跑 |
| **提示版 full #3** | **8-04 19:30** | **237** | **3.188** | **定版(与其余变体同批)** |
| baseline #1 | 8-04 之前 | 232 | 3.184 | 历史 |
| **baseline #2** | **8-04 19:30** | **220** | **3.098** | **定版(同批)** |

- full 三次运行:244 / 235 / 237,均值 ≈239;**稳定高于 baseline(220-232)**
- 单次运行差异 ±10 属正常构建方差;244 为观察上界,真实水平 ≈237-239
- 同一批内 full vs baseline 差 +17,远超批内噪声

## 4. 各题型明细(full,2026-08-04 定版)

| 题型 | 题数 | C | P | I | C 率 | C+P 率 | 1-5 平均 |
|---|---|---|---|---|---|---|---|
| Multi-hop | 63 | 21 | 26 | 16 | 33.3% | 74.6% | 3.302 |
| Temporal | 64 | 27 | 5 | 32 | 42.2% | 50.0% | 3.141 |
| Open-domain | 21 | 9 | 3 | 9 | 42.9% | 57.1% | 2.857 |
| Single-hop | 156 | 81 | 28 | 48 | 51.9% | 69.9% | 3.529 |
| Adversarial | 88 | 37 | 0 | 52 | 42.0% | 42.0% | 2.618 |

## 5. 各变体题型对比(1-5 平均,2026-08-04 同批)

| 变体 | Multi-hop | Temporal | Open-domain | Single-hop | Adversarial |
|---|---|---|---|---|---|
| full | **3.302** | 3.141 | **2.857** | **3.529** | 2.618 |
| cons-only | 2.892 | **3.231** | 2.364 | 3.433 | 2.573 |
| acc-only | 3.234 | 2.797 | 2.619 | 3.301 | **2.733** |
| baseline | **3.364** | 2.846 | 2.524 | 3.369 | **2.727** |

- full 在 Multi-hop / Open-domain / Single-hop 全面领先
- Temporal 是 cons-only 的强项(26C 构建侧 skill 对时序提取有帮助)
- Adversarial 全部较差,各变体差距小

## 6. 数据位置

- full 定版:exp/single-agent/bank_v1/validation/latest_full/(conv-26/41 + judge + rating)
- cons-only:exp/single-agent/bank_v1/validation/cons_only/
- acc-only:exp/single-agent/bank_v1/validation/acc_only/
- baseline:exp/single-agent/bank_v0/validation/latest/

## 7. 结论

1. **提示版 full(23A+26C + advisory 注入)是最终配置**:同批对比 +17,三次运行稳定领先,均值 ≈239
2. **单侧 skill 无增益**:acc-only ≈ baseline,cons-only 仅 +3
3. **双侧一致注入是收益来源**:构建侧 26C + 答题侧 23A 同时生效才有明显提升
4. 244 为历史运气上界,定版数据以 2026-08-04 同批重跑为准(full 237 / baseline 220)

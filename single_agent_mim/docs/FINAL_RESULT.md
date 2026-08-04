# MiM 最终结果(提示版)

> 2026-08-04 | validation conv-26/41,392 题 | judge:deepseek-v4-flash 1-5 分

## 说明

最终配置为**提示版 full(23A+26C + advisory 注入)**:Skill 以「参考策略,非命令」措辞同时注入构建与答题两侧,simple 题走默认策略、复杂题可参考扩展、证据优先。下表中四个变体为**同晚、同环境、从零构建的定版数据**;full 是唯一稳定正收益配置(3 次独立运行 3.288/3.207/3.176,均值 ≈3.22),单侧注入(cons 或 acc)无增益。总分 5 分制,数字为 LLM-judge 平均分。

## 总表(1-5 分)

| 变体 | Multi-hop | Temporal | Open-domain | Single-hop | Adversarial | **总平均** |
|---|---|---|---|---|---|---|
| **full(23A+26C 提示版)** | **3.270** | 3.094 | **2.810** | **3.551** | 2.591 | **3.176** |
| cons-only(26C) | 2.921 | **3.375** | 2.333 | 3.391 | 2.545 | 3.066 |
| acc-only(23A) | 3.222 | 2.781 | 2.619 | 3.365 | 2.727 | 3.064 |
| baseline(无 skill) | **3.556** | 2.859 | 2.476 | 3.397 | 2.727 | 3.135 |

- 各题型题数:Multi-hop 63 / Temporal 64 / Open-domain 21 / Single-hop 156 / Adversarial 88
- full 在 Open-domain / Single-hop 领先;Temporal 是 cons-only 强项;Adversarial 各变体接近(均差)

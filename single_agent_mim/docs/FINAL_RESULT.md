# MiM 最终结果(提示版)

> 2026-08-04 | validation conv-26/41,392 题 | judge:deepseek-v4-flash,community-standard binary CORRECT/WRONG(宽松)

## 说明

最终配置为**提示版 full(23A+26C + advisory 注入)**:Skill 以「参考策略,非命令」措辞同时注入构建与答题两侧,简单题走默认策略、复杂题参考扩展、证据优先。评测对齐社区主流(LightMem/mem0 风格):**binary CORRECT/WRONG + 宽松语义判断**(触及同主题即 CORRECT、相对时间指向同日即对、转述/额外细节不扣分)。下表为 2026-08-04 同晚、同环境、从零构建的定版数据。

## 总表(C 率,即 CORRECT 比例)

| 变体 | Multi-hop | Temporal | Open-domain | Single-hop | Adversarial | **总 C 率** |
|---|---|---|---|---|---|---|
| **full(23A+26C 提示版)** | 46% | **48%** | **57%** | **63%** | 41% | **52.6%** |
| cons-only(26C) | 34% | **52%** | 43% | 64% | 39% | 49.5% |
| acc-only(23A) | 44% | 36% | 43% | 61% | 44% | 48.7% |
| baseline(无 skill) | **50%** | 34% | 48% | 62% | **43%** | 50.3% |

题数:Multi-hop 63 / Temporal 64 / Open-domain 21 / Single-hop 156 / Adversarial 88

- full 总 C 率 52.6% 领先,但优势小于 1-5 尺度(宽松 binary 把部分正确也计入 CORRECT,压缩了差异)
- full 在 Open-domain / Single-hop / Temporal 领先;baseline 在 Multi-hop / Adversarial 略高
- 1-5 补充:full 3.176 / cons 3.066 / acc 3.064 / baseline 3.135(同批,细粒度分型)

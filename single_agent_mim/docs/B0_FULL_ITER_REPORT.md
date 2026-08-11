# b0_full_iter(bank0 空 bank + 成功经验包 v2)评测报告

> 生成日期:2026-08-10 | 判定:deepseek-v4-flash binary C/W(确定性,新 key)| runtime:qwen3-8b(新 key)| swap split | 全新隔离运行(b0_full_iter)

## 1. 运行说明

- **起点**:Bank0 空 bank(`exp/single-agent/bank_empty`),Bank0 train 数据只读复用(`empty_bank_two_phase_full_v3_20260809/train` + judge)
- **隔离**:新 config(`qwen3_8b_swap_b0.yaml`,新 API keys)、新输出目录(`outputs/b0_full_iter/`),未触碰主进程(PID 19592)任何文件
- **成功经验包 v2**:589 条无 skill 答对的 C 题,每条含**访问轨迹**(skill_trace/检索动作/可见记忆/最终证据)+ **引用记忆的构建过程**(DB result_version_id 精确关联版本链:会话/CRUD 决策/construction skill 选中)
- **迭代链**:Bank0 → train → judge → 三阶段诊断 → 成功案例 + 成功包 v2 → 候选 → CRUD → **bank1(45A+47C=92 条)**

## 2. 评测结果(swap split)

| 档 | val | conv-26 | conv-41 | test | conv-48 | conv-49 |
|---|---|---|---|---|---|---|
| Bank0(baseline 空 bank) | 53.3% | 48.7% | 58.0% | 51.3% | 54.4% | 47.4% |
| **bank1(本次)** | **52.3%** | 50.8% | 53.9% | **51.7%** | 50.6% | 53.1% |
| 新系统 Bank1(89 条,对比) | 53.6% | 46.2% | 61.1% | 54.5% | 55.2% | 53.6% |

## 3. 结论

1. **bank1(92 条)与 baseline 基本持平**:val -1.0pp / test +0.4pp
2. conv-26 有改善(48.7%→50.8%),conv-41 下降(58.0%→53.9%)——skill 增益被 conv-41 的损失抵消
3. 与用户新系统 Bank1(89 条,val 53.6%/test 54.5%)相比,本次全量+v2 成功包版本较弱(尤其 conv-41 差 7.2pp)
4. 成功经验包 v2(轨迹+构建过程)机制已跑通并生效(589 条),但第一轮增益未超 baseline——与历史观察一致:空 bank 起点的第一轮 skill 增量多为中性偏负,需要后续翻转迭代修正

## 4. 产物

- bank1:`outputs/b0_full_iter/bank1/skills/published_bank2_full`(45A+47C)
- 评测:`outputs/b0_full_iter/{val,test}/judge/summary.json`
- 成功包 v2:`outputs/b0_full_iter/success_package_v2.jsonl`(589 条)
- 脚本:`build_success_package_v2.py`、`run_full_iter_b0.py`、`configs/qwen3_8b_swap_b0.yaml`

按用户指示:本轮评测完成后停止,不跑第二轮。

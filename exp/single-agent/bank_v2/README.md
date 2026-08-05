# Bank v2(保守生成版)

> 2026-08-05 | 由 v1(23A+26C)经 V2 迭代生成:诊断修复(cons PARTIAL + answer W label)→ 337 cons 包 → 保守性 candidate/CRUD 提示词 → CRUD

## Skill Bank

- Access: 32
- Construction: 49
- 与 v1 差异:cons 侧新增 29、access 侧新增 10,均为保守约束下生成(强制不适用边界、禁止推断)

## Validation(binary CORRECT/WRONG judge,392 题)

| 指标 | v0(无 skill) | v1(23A+26C) | **v2(32A+49C)** |
|---|---|---|---|
| C | 197 | 206 | **227** |
| W | 192 | 184 | **165** |
| C 率 | 50.3% | 52.6% | **57.9%** |

- v2 在**同批代码**下比 v1 高 +6.1pp(排除 reranker 提示词混杂,归因实验确认)
- improve 66 vs regress 46,所有题型正收益

## 各题型 C 率

| 题型 | 题数 | v0 | v1 | v2 |
|---|---|---|---|---|
| Multi-hop | 63 | 49% | 44% | 49% |
| Temporal | 64 | 34% | 48% | **56%** |
| Open-domain | 21 | 48% | 57% | 57% |
| Single-hop | 156 | 62% | 63% | **69%** |
| Adversarial | 88 | 43% | 41% | **47%** |
| **总 C 率** | 392 | 50.3% | 52.6% | **57.9%** |

- Temporal +8pp、Single-hop +6pp 是 v2 最大增益
- Multi-hop 与 v0 持平(49%),v1 曾降至 44%

# MiM 正式实验数据：Bank0 与 Bank1

## 唯一版本口径

- **Bank0**：完全不加载 Skill。Runtime 仍使用相同的记忆构建、自然搜索链和回答流程，是无 Skill 基线。
- **Bank1**：第一轮正式 Skill Bank。Access 与 Construction 均属于 Bank1，但物理隔离为 `access_skill_bank_v1.json` 和 `construction_skill_bank_v1.json`。
- `v1` 只表示这两个物理文件的发布版本；召回实现不再单独占用 Bank 版本号。
- Bank1 运行时采用唯一召回流程：`name + description` 上的语义检索与 BM25 混合召回 10 个候选，再由 Qwen3-8B 适用性路由器选择 0–2 个 Skill。Construction 对会话分段后做最大池化，避免长输入截断。

## 受控 Validation 主结果

两组均使用 Qwen3-8B non-thinking、temperature 0，在相同的 LoCoMo validation（conv-26、conv-41，共 392 题）上运行。LLM as Judge 均使用 DeepSeek-V4-Flash、`locomo_semantic_judge_v2`、temperature 0，输出 `C/P/I`。

| System | Token-F1 | C | P | I | Strict Judge (`C/N`) | C+P |
|---|---:|---:|---:|---:|---:|---:|
| Bank0 | 32.63 | 165 | 52 | 175 | 42.09 | 55.36 |
| **Bank1** | **36.32** | **185** | 60 | **147** | **47.19** | **62.50** |
| **Bank1 − Bank0** | **+3.68** | **+20** | +8 | **−28** | **+5.10** | **+7.14** |

Bank1 的两段 validation 对话均从原始会话重新构建记忆，而不是复用 Bank0 或其他旧实验生成的记忆。392 条回答无 Runtime 协议错误，392 条 Judge 结果无永久错误。

## 分题型变化

| 题型 | Bank0 F1 | Bank1 F1 | F1 Δ | Bank0 Strict | Bank1 Strict | Strict Δ | C+P Δ |
|---|---:|---:|---:|---:|---:|---:|---:|
| Multi-hop | 42.72 | 50.86 | **+8.15** | 33.33 | 33.33 | 0.00 | **+14.29** |
| Temporal | 5.99 | 5.76 | −0.23 | 35.94 | 45.31 | **+9.38** | **+9.38** |
| Open-domain | 28.20 | 26.96 | −1.23 | 38.10 | 38.10 | 0.00 | +4.76 |
| Single-hop | 37.98 | 41.64 | **+3.67** | 50.00 | 55.77 | **+5.77** | +4.49 |
| Adversarial | 36.36 | 40.91 | **+4.55** | 39.77 | 45.45 | **+5.68** | +5.68 |

## 数据目录

```text
exp/
└── single-agent/
    ├── bank0/
    │   ├── train/                 # 1,200 题
    │   ├── validation/            # 392 题；受控比较基线
    │   └── test/                  # 394 题
    ├── bank1/
    │   ├── banks/
    │   │   ├── access_skill_bank_v1.json
    │   │   └── construction_skill_bank_v1.json
    │   └── validation/            # 392 题；Bank1 正式结果
    ├── paper_results.csv
    └── PAPER_RESULTS_MANIFEST.json
```

`exp/` 是 MiM 根目录下的多框架实验入口；本框架的正式数据只放在
`exp/single-agent/`。未来其他框架应与 `single-agent` 并列，不能混入
本目录。`single_agent_mim/exp` 已废弃。

# Bank1 Validation 结果

- Runtime：Qwen3-8B，non-thinking，temperature 0。
- 数据：LoCoMo validation 的 conv-26 与 conv-41，共 392 题。
- Bank：54 条 Access Skill、75 条 Construction Skill；分别存放在相邻的 `../banks/access_skill_bank_v1.json` 与 `../banks/construction_skill_bank_v1.json`。
- 召回：`name + description` 的 70% 语义相似度与 30% BM25 混合召回 10 个候选，再由 Qwen3-8B 选择 0–2 个真正适用的 Skill；允许不选。
- Construction：对完整 session 分段召回并最大池化，再使用相同的适用性路由。
- 记忆：两个对话均从 raw conversation 重新构建。
- Token-F1：36.3157%，Bank0 为 32.6308%，提升 3.6849 个百分点。
- LLM as Judge：DeepSeek-V4-Flash，C/P/I = 185/60/147；严格正确率 47.1939%，Bank0 为 42.0918%。
- 完整性：392 个唯一 QA ID，0 Runtime 协议错误，0 Judge 永久错误。

本目录只保留正式逐题结果与报告，不保存历史召回版本或消融实验。

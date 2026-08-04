# MiM Bank2 第二轮训练、诊断与发布报告

## 1. 最终结论

本轮任务已经完整执行并通过工程验收，但新增 Skill 没有通过留出验证，因此没有把负增益规则推入正式 Runtime。

正式发布的 Bank2 使用新的物理版本文件：

- `banks/access_skill_bank_v2.json`
- `banks/construction_skill_bank_v2.json`

Bank2 共 129 条正式 Skill，其中 Access 54 条、Construction 75 条。其规范化 Skill 内容哈希为：

`0efd28ed8144283fb70482995d13cc42609f3a9e9771f8f52bedb658168b7fe9`

该哈希与 Bank1 完全一致。这不是流水线没有生成新 Skill，而是无回归发布门控在四组验证中选择了稳定的 Bank1 快照，并以 Bank2/v2 的发布格式对外交付。本轮产生的新候选、CRUD 结果和三组负增益验证均已完整保留，可用于论文消融与下一轮改进。

## 2. 本轮实际完成的工作

完整链路如下：

1. 使用 Bank1 对 LoCoMo train 的 6 个对话重新运行 Qwen3-8B non-thinking Runtime。
2. 合并并保留 1,200 道题的答案、检索链、Skill trace、记忆数据库和 F1。
3. 使用 DeepSeek-V4-Flash 对 1,200 道题执行三元语义 Judge。
4. 对 Judge 判定为 P/I 的 706 道题执行 Answer、Access、Construction 三条隔离诊断链。
5. 从可修复诊断包生成抽象候选 Skill，并对空响应和结构错误执行单条断点重试。
6. 将候选按语义聚类后做批量 CRUD，形成 Access 更新快照和 Construction 更新快照。
7. 在 LoCoMo validation 的 conv-26、conv-41 共 392 道题上评测四种组合。
8. 使用相同的 DeepSeek-V4-Flash Judge 比较 C/P/I，并执行无回归选择。
9. 发布 Bank2，整理正式实验目录，修复断点续跑和 Access 预算耗尽问题。

## 3. Bank1 train 结果

| 指标 | 结果 |
|---|---:|
| 对话数 | 6 |
| QA 总数 | 1,200 |
| 唯一 QA | 1,200 |
| Runtime 协议错误 | 0 |
| Token-F1 | 33.3201% |
| Judge C | 494 |
| Judge P | 173 |
| Judge I | 533 |
| Judge 永久错误 | 0 |

各对话 F1：conv-30 为 25.2628%，conv-42 为 34.4372%，conv-43 为 31.0367%，conv-44 为 39.9761%，conv-48 为 33.7754%，conv-49 为 33.0531%。

## 4. 诊断结果

Judge 的 173 个 P 与 533 个 I 构成 706 个诊断入口。

| 诊断链 | 已处理 | 关键结果 | 可修复包 | 模型错误 |
|---|---:|---:|---:|---:|
| Answer Failure | 706 | 265 个回答侧错误 | 不生成 Skill 包 | 0 |
| Access Failure | 706 | 171 个检索问题 | 149 | 0 |
| Construction Failure | 706 | 193 个构建问题 | 182 | 0 |

Construction 中另有 236 个 `data_error`：原始 LoCoMo evidence 本身不足以完整支持 reference answer。它们被保留为数据质量记录，不进入 Construction Skill 生成，避免用 Skill 修复数据集证据缺口。

三条诊断链符合既定职责边界：

- Answer 先判断“已有检索上下文是否足够但模型仍答错”。
- Access 只比较当前记忆中的相关条目与实际搜索链，不读取其他版本记忆。
- Construction 从 evidence 反向追踪相关记忆，沿消息来源和版本变更定位第一个错误点；多个错误只处理最早错误。

## 5. 候选生成与 CRUD

| 阶段 | Access | Construction |
|---|---:|---:|
| 可修复诊断包 | 149 | 182 |
| 生成候选 | 92 | 136 |
| 判断无需新 Skill | 57 | 46 |
| 最终未解决生成错误 | 0 | 0 |
| CRUD 后新增正式 Skill | 35 | 16 |

内部快照规模：

| 内部快照 | Access | Construction | 总数 | 含义 |
|---|---:|---:|---:|---|
| v000 | 54 | 75 | 129 | Bank1 稳定基座 |
| v001 | 89 | 75 | 164 | Access 增量 |
| v002 | 89 | 91 | 180 | Access + Construction 增量 |

这里的 v000/v001/v002 仅是本轮可变仓库的内部快照，不是对外 Bank 编号。对外只使用 Bank1 与 Bank2，以及物理隔离的 `access_skill_bank_vN.json`、`construction_skill_bank_vN.json`。

## 6. 四组留出验证

全部使用同一组 392 道 validation 问题、Qwen3-8B non-thinking、temperature 0，以及 DeepSeek-V4-Flash 三元 Judge。

| 候选 | Access | Construction | F1 | C | P | I | C 比例 | C+P | 结论 |
|---|---|---|---:|---:|---:|---:|---:|---:|---|
| Bank1 stable | Bank1 | Bank1 | **36.3157%** | **185** | 60 | **147** | **47.1939%** | **62.5000%** | 选中 |
| Access-only | 新增量 | Bank1 | 33.7746% | 174 | 63 | 155 | 44.3878% | 60.4592% | 拒绝 |
| Access + Construction | 新增量 | 新增量 | 31.0776% | 156 | 59 | 177 | 39.7959% | 54.8469% | 拒绝 |
| Construction-only | Bank1 | 新增量 | 33.3128% | 151 | 64 | 177 | 38.5204% | 54.8469% | 拒绝 |

所有四组均为 392/392 唯一 QA、0 个 Runtime 协议错误、0 个 Judge 永久错误。

自动选择标准为：先最大化 C 比例，再最小化 I 比例，最后最大化 Token-F1。内部 v000 在三个指标上都优于 v001 与 v002；额外的 Construction-only 正交消融同样没有超过 v000。

## 7. 为什么本轮 Skill 会下降

### 7.1 Access 增量存在“挤出效应”

35 个新增 Access Skill 在 validation 的 141 道题中至少被选择过一次。对这些题进行严格配对比较：

- F1 上升 19 题、下降 35 题、持平 87 题；平均 F1 变化为 -6.4477 个百分点。
- Judge 等级上升 13 题、下降 24 题、持平 104 题。

新 Skill 并非只提供额外提示。Runtime 最多注入 2 条 Skill，因此描述较宽的新规则会占据有限槽位，挤掉原本更可靠的 Bank1 Skill。高频新规则包括 class/group enrollment、past activity action verbs、pet/family additions、motivation retrieval 等；它们具有可复用形式，但适用条件仍不够窄。

### 7.2 Construction 增量改变了大量上游记忆

Construction-only 与 Bank1 使用完全相同的 Access Bank，因此差异只来自新记忆。逐题比较结果：

- F1 上升 66 题、下降 75 题、持平 251 题，平均变化 -3.0029 个百分点。
- Judge 等级上升 42 题、下降 80 题、持平 270 题。

这说明 Construction 候选虽然来自真实 failure，但批量上线后对正常会话的触发范围过宽，新增或改写的记忆会影响大量非目标问题。只在失败包上判断“规则合理”不足以证明它对正常样本无害。

### 7.3 当前缺少候选级反事实门控

现有流程在候选生成阶段检查抽象性、格式、重复和职责边界，在最终阶段检查整个 Bank，但没有在每条新 Skill 或每个语义组发布前执行“启用/禁用该增量”的配对重放。因此，一批局部合理但总体有害的规则会一起进入候选快照，最后只能整批回退。

下一轮最重要的改进不是继续扩大生成预算，而是增加候选级或语义组级反事实验证：

1. 对触发该 Skill 的 failure replay 验证是否修复。
2. 从历史正确样本召回相似的 positive replay，验证是否造成回归。
3. 只有 `修复收益 > 回归损失 + 门槛` 时才允许进入正式 Bank。
4. 对 Access Skill 记录其是否挤掉原 Skill，并校准 description 的触发范围。
5. 对 Construction Skill 比较应用前后的记忆 diff，限制一次规则影响的消息范围和记忆数量。

## 8. 本轮修复的工程问题

### 8.1 Access 预算耗尽不再产生协议错误

旧逻辑在搜索动作达到上限且模型尚未输出 answer 时，直接返回 `Budget exhausted without answer`。现在搜索/检查预算耗尽后会增加一次不允许调用工具的 answer-only 最终回合；模型必须基于完整搜索历史回答，证据不足时返回 `No information available.`。该回合不增加搜索预算。

本轮两条真实失败题经此修复后均单题续跑成功，最终所有验证组协议错误为 0。

### 8.2 Skill 流水线续跑不再重复 CRUD

旧版 `--resume --stage all` 会重新处理已发布候选。现在每一侧在续跑时检查发布事务及其对应快照：二者完整则复用，事务指向缺失快照则立即报错，不会静默重复更新。

实测续跑输出：Access 复用 v001，Construction 复用 v002，Bank 快照数量保持不变。

### 8.3 并发与断点

- DeepSeek-V4-Flash 使用三个独立 API 通道轮询，Judge 最高 24 workers。
- Answer、Access、Construction 诊断均支持逐条恢复。
- validation 先构建每个版本/对话的冻结记忆，再复制为独立 QA 分片。
- 单条空响应、模型格式错误或预算耗尽只重跑对应题目，不重跑完整对话。

最终完整测试结果：`68 passed`。

## 9. 正式数据目录

```text
exp/single-agent/
├── bank1/
│   ├── train/                         # 1,200 QA + Flash Judge
│   ├── diagnosis/                     # 三条隔离诊断链与 331 个修复包
│   └── banks/                         # Bank1 Runtime 文件
└── bank2/
    ├── banks/
    │   ├── access_skill_bank_v2.json
    │   └── construction_skill_bank_v2.json
    ├── build/
    │   ├── candidates/                # 92 Access + 136 Construction 候选
    │   ├── transactions/              # 两侧 CRUD 发布事务
    │   ├── official/                  # v000/v001/v002 与自动选择记录
    │   ├── source_diagnoses.json
    │   └── summary.json
    └── validation/
        ├── all.jsonl                  # 选中稳定快照的 392 条答案
        ├── judgments_flash.jsonl      # 选中快照的 392 条 Judge
        ├── summary.json
        ├── judge_flash_summary.json
        ├── comparison.csv
        └── candidates/
            ├── access_only/
            ├── access_construction/
            └── construction_only/
```

`single_agent_mim/outputs/` 保留运行期分片、SQLite 状态和调试归档；论文引用与交接应以根目录 `exp/single-agent/` 下的正式数据为准。

## 10. 发布判断

本轮不能声称新 Skill 提升了性能。可以准确声称：

1. 完成了一轮从 train failure、三路诊断、候选生成、CRUD 到留出验证的闭环。
2. 三种新增组合均在独立 validation 上回归。
3. 无回归门控成功阻止了有害 Skill 上线。
4. Bank2 是经过第二轮重新验证的稳定发布，而不是未经验证的候选堆叠。
5. 本轮负结果明确指出下一步应研究候选级反事实 replay 与 Skill 触发校准。

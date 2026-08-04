# Skill Bank Round 1 下降诊断与修复报告

日期：2026-08-01  
范围：`single_agent_mim` 的 Round 1 Skill 生成、CRUD、运行时召回、Bank 选择与 DeepSeek 维护模型

## 1. 结论

Round 1 的下降不是单一提示词问题，而是四个工程问题叠加：

1. **Skill 没有真正完成抽象。** 57 个 Access Skill 全部只来自一个诊断；43 个 Construction Skill 中有 37 个也只来自一个诊断。正式 Bank 实际上接近“错误案例改写库”，而不是可复用规则库。
2. **运行时强制注入 Top-3。** 即使所有候选都不相关，也一定注入三个 Skill。测试阶段共发生 1182 次 Access Skill 注入，其中 985 次得分低于 0.20。
3. **发布前没有可执行的质量门。** 校验器只能检查长度、ID 和答案泄漏，不能发现“不支持的 memory_kind”“抽取阶段要求 UPDATE”“允许无证据推断”等运行契约冲突。
4. **v002 从未通过验证集选择。** Round 1 目录没有 validation 运行、Judge 结果或 `skills/selection.json`。原 Stage F 也无法成功执行，因此 `selected.json` 指向的是最新 v002，而不是验证集胜出的版本。

因此，本轮没有把 v002 继续包装成“有效学习结果”。当前正式选择已回退到空 Bank `v000`，以恢复 Base 行为；`bank_v001.json` 和 `bank_v002.json` 保留不变，供后续诊断和论文消融使用。

## 2. Round 1 数据复核

### 2.1 整体结果

| 指标 | Base | v002 MiM | 变化 |
|---|---:|---:|---:|
| Token F1 | 0.3491 | 0.3485 | -0.0006 |
| Judge C | 164 | 161 | -3 |
| Judge P | 68 | 59 | -9 |
| Judge I | 162 | 174 | +12 |
| Judge C+P | 58.9% | 55.8% | -3.0 个百分点 |

394 个问题中：

- 66 个问题的 Judge 等级提高；
- 70 个问题的 Judge 等级下降；
- 258 个问题不变；
- 43 个问题由 C 直接降为 I，而 I 升为 C 的问题只有 32 个。

这说明 Skill 并非完全无效，但负迁移超过了正迁移。

### 2.2 类别表现

Cat3 时间问题和 Cat5 对话理解分别提高 5.0 与 4.7 个百分点，说明部分时间解析、跨信息搜索规则具有价值。Cat1 单跳事实下降 13.5 个百分点，说明简单问题被额外规则干扰最严重。一个正常的 Skill 系统应当在没有适用规则时退回原行为，而不是让复杂策略覆盖简单检索。

### 2.3 Skill 抽象失败

| 侧 | 正式 Skill 数 | 仅一个候选支持 | 单例比例 |
|---|---:|---:|---:|
| Access | 57 | 57 | 100% |
| Construction | 43 | 37 | 86.0% |

这与预期的“多个相似诊断先形成 candidate，再由 CRUD 合并为少量稳定规则”不一致。原 CRUD 提示还用“空 Bank 中两个 candidate 各建一个 Skill”作为示例，模型自然倾向于一对一发布。

### 2.4 召回没有拒绝选项

原实现使用 `0.85 × semantic + 0.15 × lexical` 排序，但无最低分：

- 每个问题固定注入 3 个 Access Skill；
- 选中分数中位数只有 0.141；
- 1182 次注入中，985 次低于 0.20；
- 最高频 Access Skill 在 394 个问题中出现 80 次，远超其窄触发范围。

例如，“根据体裁偏好推断是否喜欢作者作品”被触发 80 次；“课程或活动报名检索”被触发 52 次并累计产生明显负向 Judge 变化。这不是这些规则在所有场景下必然错误，而是其描述/content 中的通用词让它们在不适用的问题上被强制选中。

### 2.5 Construction 的真实情况

交接文档推测 Construction Skill 未接入，这一判断不成立。代码和 trace 均显示：每个 session 构建前会召回 Construction Skill，并同时注入候选抽取与 CRUD 两个阶段。QA 的 `skill_ids` 只记录 Access 阶段，因此不能用该字段判断 Construction 是否生效。

不过 Construction 侧确实发生了过度构建：

| 对话 | Base 活跃记忆 | v002 活跃记忆 | 增量 |
|---|---:|---:|---:|
| conv-47 | 153 | 164 | +11 |
| conv-50 | 120 | 139 | +19 |

部分 Skill 要求“抽取每一个离散事实”“不确定时优先保留”“为 ordinal achievement 新建 metric/attribute memory”，与当前紧凑记忆和允许类型契约冲突，会增加碎片与噪声。

### 2.6 验证阶段实际未运行

原 Stage F 存在三个阻断错误：

1. 把 `ModelConfig` 直接传给 `MiMEvaluator`，而 Runtime 需要的是 `ModelClient`；
2. 从只包含 Token F1 的 `EvalReport` 读取不存在的 `by_category` C/P/I 字段；
3. F1 tie-break 将 `overall_f1` 与 Bank 版本号比较，而不是与历史最佳 F1 比较。

所以 Round 1 的 `selected.json = v002` 只是发布过程留下的最新版本指针。交接文档中“验证集按 C、I、F1 选择”的流程目标是正确的，但没有在产物中发生。

## 3. 已完成修复

### 3.1 运行时召回可退出

- Runtime Skill 只使用 `name + description` 进行触发匹配；`content` 只在触发后作为执行指令，不再参与触发语义。
- Access 与 Construction 新增 `skill_min_score`，当前配置为 `0.20`。
- `top_k=3` 现在表示最多三个，而不是必须三个；没有 Skill 达标时注入空列表，完全退回 Base 工作流。
- trace 新增 `min_score` 和 `scored_fields`，并继续披露低于门槛的 near misses，便于下一轮诊断。

按 Round 1 查询做离线重排投影，在尚未重跑模型的前提下，Access 通过 0.20 门槛的候选约为原注入次数的 19.5%，Construction 约为 29.0%。这是召回量变化估计，不是新的效果分数。

### 3.2 候选生成补全真实系统契约

英文 `candidate_generation.md` 现在明确说明：

- Access 已具备持续上下文中的多步 ReAct、五类检索、扩展、过滤、时间、深度、检查和充分性判断；
- Construction 已具备完整 session 抽取和批量 CRUD，只允许六种 memory_kind；
- Skill 必须是默认规则之外的最小增量，且未来能从问题/session 本身判断是否触发；
- 模型随机性、已有基础规则、依赖 gold 才能触发的问题应返回 NO_CHANGE；
- 按主题拆成“书籍 Skill、宠物 Skill、课程 Skill”通常属于过拟合，应抽象为共同的失败机制。

### 3.3 CRUD 改为“最小规则库”

英文 `batch_crud.md` 现在要求：

- 同一失败机制必须合并，不得一候选一正式 Skill；
- 新增 Skill 通常至少由两个独立 candidate 支持，并完整写入 `source_candidate_ids`；
- 单例可以更新已有、已经获得支持的 Skill，但不能直接创建新的正式 Skill；
- 重复基础能力、无精确触发/停止条件、违反 Runtime 契约或允许无证据推断的候选必须拒绝。

同时将聚类目标批次从 8/10 调整为 12/20，给同一语义簇更大的统一 CRUD 空间，降低相似 candidate 被切到不同小批次后各建一个 Skill 的概率。

### 3.4 发布前确定性质量门

即使 CRUD 模型没有遵守提示，代码仍会阻止支持数小于 2 的 `add_skill` 进入正式 Bank，并把对应 resolution 改为 `REJECTED`。已有 Skill 的增量更新不受这一限制。

校验器新增 side-aware 契约检查，可拦截：

- Construction 使用 `metric`、`attribute`、`fact`、`intention` 等不支持类型；
- Construction 要求自动补全所有可能地点等无证据推断；
- Access 直接绕过证据检索返回 gold/reference answer 的规则。

### 3.5 Validation + Judge 选择链修复

新的 Stage F 会对每个不可变 Bank 版本执行：

1. 在 validation 两个对话上重新构建记忆并回答；
2. 自动调用统一的 DeepSeek C/P/I Judge；
3. 检查 Judge 行数必须与 QA 行数完全一致；
4. 按 `最高 C rate → 最低 I rate → 最高 Token F1` 选择；
5. 只有完整通过后才更新 `selected.json`；
6. 中途异常会恢复进入 Stage F 前的选择，不留下半完成 Bank。

当前 Round 1 因没有 validation 产物且测试集已证明回归，正式选择已回退：

```text
outputs/skill_bank_v1/skills/official/selected.json -> v000
```

v001/v002 没有删除或修改。

## 4. DeepSeek V4 Flash 切换

所有当前可执行维护入口已经从 `deepseek-v4-pro` 切换为精确模型 ID `deepseek-v4-flash`：

- `configs/default.yaml`
- `configs/qwen3_8b_dashscope.yaml`
- `scripts/judge_predictions.py`

Candidate 生成与 Judge 使用 Flash thinking/high；CRUD 使用 Flash non-thinking。需要特别注意，DeepSeek V4 默认开启 thinking，因此 CRUD 不能再用空 `extra_body` 表示关闭，而是显式传：

```json
{"thinking":{"type":"disabled"}}
```

现有 API 已完成实际冒烟：配置模型、响应模型均为 `deepseek-v4-flash`，JSON 内容正常。

官方资料确认模型 ID、OpenAI 兼容 base URL、JSON 支持、1M 上下文及 thinking 开关：

- [DeepSeek 模型与价格](https://api-docs.deepseek.com/quick_start/pricing)
- [DeepSeek Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode)
- [DeepSeek Models API](https://api-docs.deepseek.com/api/list-models)

## 5. 验证状态

已完成：

- 58 个离线测试全部通过；
- 关键 Python 文件通过 `py_compile`；
- DeepSeek V4 Flash API 冒烟通过；
- Round 1 正式选择回退到 v000；
- v001/v002 和所有原始评测产物保留。

尚未宣称：

- 修复后的 Bank 已提升 validation/test 分数。

原因是新规则需要重新生成候选和 CRUD，不能拿测试集上的负向 Skill 名单直接删改并当作训练结果，否则会造成测试泄漏。当前能确定的是“坏 Bank 不再上线”和“相同结构性错误被代码阻断”；实际增益必须由新的 validation-only 选择给出。

## 6. Round 2 推荐执行

在 `single_agent_mim` 目录运行：

```powershell
$env:HF_HUB_OFFLINE='1'

python scripts/run_skill_bank_pipeline.py `
  --config configs/qwen3_8b_dashscope.yaml `
  --diagnosis-run outputs/diagnosis/deepseek_v4_pro_diag_v3 `
  --run-id skill_bank_v2 `
  --workers 4 `
  --stage candidates

python scripts/run_skill_bank_pipeline.py `
  --config configs/qwen3_8b_dashscope.yaml `
  --diagnosis-run outputs/diagnosis/deepseek_v4_pro_diag_v3 `
  --run-id skill_bank_v2 `
  --workers 4 `
  --stage crud `
  --resume

python scripts/run_skill_bank_pipeline.py `
  --config configs/qwen3_8b_dashscope.yaml `
  --diagnosis-run outputs/diagnosis/deepseek_v4_pro_diag_v3 `
  --run-id skill_bank_v2 `
  --workers 4 `
  --stage validate `
  --resume
```

Stage validate 已包含 DeepSeek Flash Judge，不需要另跑 Judge。只有 `outputs/skill_bank_v2/skills/selection.json` 存在、每个候选版本的 Judge 行数完整、且选中版本不是因为运行中断留下的临时指针，才可进入 test。

正式 test 应重新跑 Base 和选中 MiM，并同时报告 Token F1 与 Judge C/P/I。测试集只用于最终报告，不参与阈值、Skill 删除或 Bank 版本选择。

## 7. 后续判断标准

Round 2 至少应同时满足：

1. 正式 Bank 中单例新 Skill 数为 0；
2. 平均每题实际注入 Skill 数显著低于上限 3；
3. Cat1 不再出现大幅下降；
4. validation 上选中版本的 C rate 高于或等于 v000，I rate 不高于 v000；
5. test 上同时给出 Base/MiM 的 C、P、I、C+P、Token F1 和分类结果；
6. Construction 记忆数量增长必须能由正确率收益解释，不能只追求“记得更多”。

如果没有任何版本胜过 v000，系统应正式选择 v000。这不是训练失败处理异常，而是 Skill Bank 的正确拒绝机制。

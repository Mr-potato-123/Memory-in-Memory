# 诊断包初步质量报告

## 一、检查范围

本次检查对象为已经停止的第一版 Diagnosis-only 运行。

- 对话：`conv-30`
- Token-F1 低于 0.5 的候选错误题：74 道
- 实际检查的完整诊断包：59 个
- Runtime 没有重跑
- Skill-Maker 没有运行

由于第一版诊断使用了中文提示词，而且诊断包存在多项质量问题，部分运行目录
已经删除。保留本报告作为问题记录，不能继续恢复第一版运行。

## 二、总体结论

底层的确定性数据准备基本可用，但第一版生成的诊断结论和修复包还不能直接交给
Skill-Maker 自动学习。

目前做得比较好的部分：

- 保存了 Access 每一次搜索或查看动作及完整返回结果；
- 保存了与标注证据相关的当前记忆版本；
- Construction 包含准确的原始证据消息；
- 保存了候选、决策、SKIP、版本变化和当前快照；
- 对于部分 UPDATE 错误，能够给出修改前后的版本编号；
- Access、Construction 和 Answer Check 分别输出，没有合并成一个错误标签。

暂时不能直接使用的主要原因：

1. Diagnosis 提示词是中文，不符合现在确定的全英文模型提示词规范；
2. 59 个诊断包中有 9 个至少包含一次模型调用错误，却仍被标记为完成；
3. 12 个 Access 问题中有 4 个没有生成修复包；
4. 33 个 Construction 问题中有 15 个没有生成修复包；
5. Construction 返回了不统一的错误阶段名称，导致很多本可自动处理的结果进入
   人工复核；
6. Answer Check 没有把记忆的世界时间提供给模型，日期题出现明显误判；
7. 仅用 Token-F1 筛选错误题，会把语义正确、只是格式不同的答案送入诊断；
8. Skill-Maker 原先只读取简化摘要，没有真正读取完整修复包。

## 三、Access 诊断质量

第一批结果分布：

```text
没有检索问题：45
缺少必要记忆：5
返回了冲突信息：7
Access 模型错误：2
```

旧逻辑把“搜索结果中存在干扰项或冲突项”也当作 Access failure。这个定义与当前
设计不一致。

按照现在确认的定义，Access Agent 只需要判断：

> 在当前快照中确实存在、并且回答问题所必要的记忆，是否曾经被自然搜索链返回。

只要必要记忆已经全部返回，后续选错证据、使用错日期或推理错误就不应该继续生成
Access Skill。冲突记忆可以保留为审计信息，但不能单独触发 Access 修复。

对于真正的“缺少必要记忆”问题，当前底层数据已经比较充分：

- 问题、参考答案和 Runtime 预测；
- 与证据消息关联的当前记忆；
- 完整搜索请求和返回结果；
- 必要、已返回和缺失的版本编号；
- 缺失记忆的完整内容；
- 诊断原因。

在分类规则修正后，这类包可以支持 Access Skill 生成。

## 四、Construction 诊断质量

第一批结果分布：

```text
提取错误：28
更新时丢失信息：2
候选内容错误：1
错误合并：1
错误跳过：1
没有构建问题：19
原始数据问题：5
Construction 模型错误：2
```

最大的工程问题是错误阶段名称不统一。模型输出过：

```text
candidate_generation
candidate
update
memory_update
merge
```

而程序只接受：

```text
extraction
update_loss
wrong_merge
```

这导致 33 个 Construction 问题中有 15 个被标记为需要人工复核，并且没有生成
修复包。

对于阶段名称合法的样本，当前 Construction 包已经包含：

- 原始证据消息；
- 消息进入构建的 commit；
- 候选和决策；
- 初始记忆版本；
- 后续版本变化；
- 第一个错误涉及的编号；
- UPDATE 的前后版本；
- 对错误原因的说明。

因此底层数据总体足以定位第一个 Construction 错误。问题主要出在模型输出规范和
程序归一化，而不是 SQLite 中缺少溯源信息。

## 五、Answer Check 质量

第一批结果：

```text
判断正确：8
判断错误：46
模型调用错误：5
```

旧版 Answer Check 只向强模型提供：

- memory version ID；
- memory kind；
- memory content。

它没有提供：

- `subject`；
- `world_start`；
- `world_end`。

这会直接破坏日期题。

一个明确例子：

```text
问题：Jon 和 Gina 是什么时候决定合作制作舞蹈内容的？
参考答案：21 July 2023
Runtime 预测：2023-07-21
```

Runtime 预测在语义上完全正确，而且检索到的记忆中存在
`world_start=2023-07-21`。旧版 Answer Check 看不到这个字段，因此错误地回答
“信息不足”。

## 六、Token-F1 带来的假错误

当前 Diagnosis runner 只要看到：

```text
Token-F1 < 0.5
```

就会把问题送入诊断。

这会把以下情况误认为系统错误：

- 日期格式不同；
- 合理同义改写；
- 简写与全称不同；
- 列表顺序不同但内容一致。

因此下一次运行前，需要先对 Runtime 原始预测做语义正确性判断。这个判断与
“强模型能否根据已检索记忆重新回答”不是同一件事，应该分别记录。

建议记录两个字段：

```text
runtime_prediction_correct
maintenance_can_answer_from_returned_memory
```

如果 Runtime 原始预测已经语义正确，就不应该进入 Failure Diagnosis。

## 七、修复包是否足够

结论需要分两层看。

### 底层原始数据

基本足够。

Access 有搜索链和相关记忆，Construction 有原始消息、候选、决策和版本变化，
已经具备工程定位能力。

### 第一版自动生成的 repair package

不够稳定。

主要表现为：

- 部分问题没有 repair package；
- 阶段名称不合法时被直接拦截；
- 模型错误仍被标记为 completed；
- Answer Check 缺少时间字段；
- Skill-Maker 没有使用修复包中的完整内容。

所以第一版结果不能直接用于自动生成或发布 Skill。

## 八、已经完成的修正

当前代码已经完成以下修改，但尚未重新运行 Diagnosis：

1. Access 只有缺失必要且实际存在的记忆时才进入修复；
2. 干扰项或冲突项不再单独触发 Access Skill；
3. Construction 提示词限定合法阶段名称；
4. 程序会把常见阶段别名归一化；
5. Answer Check 会读取 subject、world_start 和 world_end；
6. Skill-Maker 会读取缺失记忆、搜索请求、原始消息、候选和前后版本；
7. 组件模型错误会标记为 `partial_model_error`，允许重新运行；
8. Diagnosis 提示词已全部改成英文。

## 九、下一次运行前仍需完成

最重要的剩余工作是：

1. 给 Runtime 原始预测增加独立的语义正确性门槛；
2. 对五个样本做新的冒烟测试；
3. 确认每个需要修复的问题都有非空 repair package；
4. 确认没有组件模型错误被标记为 completed；
5. 人工检查日期、列表、多跳和反事实问题各至少一个；
6. 冒烟测试通过后再启动全部 train 数据。

## 十、最终决定

不恢复第一版 Diagnosis。

保留六个 Runtime 源运行，完成剩余语义门槛后，使用新的 run ID 从头启动第二版
Diagnosis。

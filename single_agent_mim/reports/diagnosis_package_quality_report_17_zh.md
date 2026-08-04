# 首批 17 个 Judge-First 诊断包质量审查报告

## 1. 审查结论

本报告审查的是用户确认时已经生成的前 17 个诊断样本。固定样本如下：

- `conv-30`: `qa_0000`、`qa_0002`、`qa_0003`、`qa_0005`、`qa_0006`
- `conv-42`: `qa_0000`、`qa_0001`、`qa_0002`、`qa_0003`
- `conv-43`: `qa_0000`、`qa_0001`、`qa_0002`
- `conv-44`: `qa_0000`、`qa_0002`
- `conv-48`: `qa_0000`、`qa_0001`、`qa_0002`

后续继续生成的样本不计入本报告。

总体结论：

> 这批诊断包的“证据准备”和“成功解析后的诊断内容”已经具备实际价值，但工程稳定性、二元 Judge 契约、物理隔离和独立重试仍不合格。当前结果适合人工研究和挑选有效修复案例，不适合未经复核直接批量交给 Skill-Maker 自动生成或修改 Skill。

需要区分两个层面：

1. **诊断内容本身并不差。**
   成功返回的 Construction 诊断大多能够定位最早发生的构建错误；成功返回的 Access 诊断也大多能够正确区分“记忆存在但没有召回”和“记忆根本没有正确构建”。
2. **诊断流水线的工程完成度较差。**
   17 个样本中只有 10 个同时获得有效的 Access 与 Construction 结果；其余 7 个至少有一个核心诊断 Agent 返回了无效 JSON。当前程序仍把这些样本标记为整体 `completed`，并且没有为失败的单个 Agent 提供独立重试。

综合评级：**C，内容可研究，尚不可自动化交付。**

---

## 2. Judge 路由的实际情况

### 2.1 新入口没有使用 F1

实际运行入口是：

`scripts/run_judge_first_diagnosis.py`

它读取：

`outputs/nsc_train_all_judge.jsonl`

并根据 LLM-as-Judge 的标签决定是否进入诊断。代码没有读取或使用 Token-F1 进行筛选。之前基于旧入口 `run_diagnosis_only.py` 得出的“仍在使用 F1 路由”的判断不适用于这 17 个包，现予以撤回。

### 2.2 当前 Judge 实际仍是三标签，但运行时相当于二元路由

当前 Judge 输出：

- `C`: correct
- `P`: partial
- `I`: incorrect

诊断程序跳过 `C`，将 `P` 和 `I` 全部送入诊断。因此在路由效果上，它等价于：

- `C` → 正确，不诊断
- `P/I` → 不正确，进入诊断

所以它没有使用 F1，也没有因为 `P` 与 `I` 的区别改变诊断路径。不过，这仍不符合最新约定的简洁接口。目标接口应该只保留：

```json
{
  "qa_id": "...",
  "correct": false,
  "reason": "Brief semantic judgment."
}
```

当前的 `strict_score`、`partial_aware_score` 和 `P` 标签都不是诊断路由所必需的。

### 2.3 17 个 Judge 结果的人工复核

17 个样本中：

- 10 个被标记为 `I`
- 7 个被标记为 `P`
- 运行时全部作为“不正确”进入诊断

人工复核结果：

- **15 个可以明确认定为错误回答**
- **1 个是明确的 Judge 假阳性**
- **1 个是边界样本，建议人工确认**

明确的假阳性：

| 样本 | Prediction | Reference | 判断 |
|---|---|---|---|
| `conv-48_qa_0002` | `a few years ago` | `a few years before 2023` | 两者在该会话时间背景下语义等价，应判为正确 |

边界样本：

| 样本 | Prediction | Reference | 判断 |
|---|---|---|---|
| `conv-30_qa_0002` | `dance class, contemporary dance, and taking breaks to destress` | `by dancing` | 已包含核心答案“通过跳舞”，但混入额外内容且没有清楚说明两个人都如此；严格判错有依据，但也可能属于 Judge 过严 |

因此，Judge 在这 17 个样本上的明确错误样本筛选精度至少为：

```text
15 / 17 = 88.2%
```

如果将边界样本也视为错误回答，则为：

```text
16 / 17 = 94.1%
```

结论是：Judge 可以作为前置筛选器，但当前不能假设其判错结果百分之百可靠。对于后续要自动生成 Skill 的样本，至少应过滤掉“两个诊断 Agent 都没有发现问题”的情况，并对语义近似答案增加一次确定性复核。

---

## 3. 17 个诊断包的完整性

### 3.1 核心结果统计

| 项目 | 数量 | 比例 |
|---|---:|---:|
| 样本总数 | 17 | 100% |
| Access 成功解析 | 15 | 88.2% |
| Access 返回 `model_error` | 2 | 11.8% |
| Construction 成功解析 | 11 | 64.7% |
| Construction 返回 `model_error` | 6 | 35.3% |
| Access 与 Construction 均成功 | 10 | 58.8% |
| 至少一个核心 Agent 失败 | 7 | 41.2% |
| Access、Construction、AnswerCheck 全部成功 | 9 | 52.9% |

这里的“成功”仅表示输出通过结构解析，不代表诊断一定正确。

### 3.2 失败样本

以下 7 个样本至少缺少一个核心诊断结果：

| 样本 | Access | Construction | 可保留内容 |
|---|---|---|---|
| `conv-30_qa_0000` | 无效 JSON | 成功 | Construction 可用 |
| `conv-30_qa_0003` | 成功 | 无效 JSON | Access 可用 |
| `conv-42_qa_0001` | 成功 | 无效 JSON | Access 可用 |
| `conv-43_qa_0000` | 成功 | 无效 JSON | Access 可用 |
| `conv-43_qa_0001` | 成功 | 无效 JSON | Access 可用 |
| `conv-44_qa_0000` | 成功 | 无效 JSON | Access 的“无检索问题”结论可用 |
| `conv-48_qa_0001` | 无效 JSON | 无效 JSON | 两条诊断均不可用 |

这说明独立诊断的思想是有价值的：即使一侧失败，另一侧的结果仍可能有效。但当前物理存储和恢复逻辑没有真正利用这种独立性。

---

## 4. Access Failure 诊断质量

### 4.1 优点

Access Agent 的输入边界基本正确：

- 不读取原始对话正文；
- 只读取与标注 evidence 反向关联的快照记忆；
- 读取完整的自然搜索链；
- 检查必要记忆是否曾经在任一步搜索或检查中返回；
- 不要求模型生成检索关键词、权重或过滤条件；
- 不因为 Construction 同时有问题就跳过 Access 判断。

成功解析的 15 个 Access 报告中，绝大多数结论具有明确的版本 ID、搜索步骤和缺失列表。例如：

- `conv-30_qa_0005` 准确指出包含 Marley flooring 的 `mem_conv-30_0015_v1` 存在但没有被召回；
- `conv-30_qa_0003` 准确指出关于两人失业并创业的三条必要记忆全部没有进入搜索链；
- `conv-43_qa_0000` 准确区分了“赢得冠军”已召回与“提高投篮命中率”未召回；
- `conv-43_qa_0002` 正确判断 sneakers 和 fantasy DVDs 在最终快照中已经缺失，因此不能归咎于检索。

这些报告已经能够作为 Access 修复案例的基础。

### 4.2 一个明确的 Access 误判

`conv-42_qa_0000` 的 Access 报告不正确。

问题询问 Nate 是否可能还有 Joanna 之外的朋友，参考答案依据是他的游戏队友。最终快照中的相关记忆：

```text
mem_conv-42_0001_v3
Nate won his fourth video game tournament ... and earned money from gaming.
```

这条最终记忆已经不再包含 team 或 teammates。仅仅参加电子游戏比赛并不能可靠推出他一定有队友，因为比赛也可能是个人项目。

Access Agent 却把该记忆认定为回答问题所必需的有效快照记忆，并因为它未被检索而判为 Access Failure。这是错误的。正确结论应为：

- 当前快照中不存在能够支持“有游戏队友”的有效记忆；
- 因此不存在“有用记忆存在但没被检索”的 Access Failure；
- 真正问题是 Construction 在 commit 17 更新时删除了原记忆中的 team 信息。

这也是一个很重要的边界规则：

> “来源上与 evidence 有关联”不等于“当前版本的正文仍然支持答案”。Access Agent 必须按当前版本内容判断，而不能按血缘关系或模糊推断判断。

### 4.3 Access 质量结论

在 15 个成功解析的 Access 报告中：

- 14 个结论可接受；
- 1 个存在明确的语义误判；
- 人工复核准确率约为 `14/15 = 93.3%`。

但 2 个 Access 调用直接返回无效 JSON，所以若按全部 17 个样本衡量，可直接使用的 Access 结果为 `14/17 = 82.4%`。

---

## 5. Construction Failure 诊断质量

### 5.1 优点

成功解析的 Construction 报告整体质量较高。它们能够遵循“只定位最早错误”的规则，而不是一次罗列全部后果。

典型正确案例：

#### `conv-30_qa_0000`

原始消息发生于 2023-01-20，并说失业发生在 `yesterday`。候选记忆把日期写成 2023-01-20，而不是 2023-01-19。

诊断正确定位到：

- stage: `extraction`
- commit: `1`
- candidate: `cand_conv-30_conv-30_s01_000`
- first persisted version: `mem_conv-30_0001_v1`

#### `conv-30_qa_0005`

原始消息明确出现 “ideal dance studio by the water”，但没有候选记忆提取这一信息。

诊断正确定位为 commit 1 的 `extraction` 遗漏。虽然 Marley flooring 也与答案有关，但它已经被构建，只是没有召回，因此两个独立 Agent 分别发现了不同问题。

#### `conv-42_qa_0000`

初始版本明确保留了 “with his team”，commit 17 的 UPDATE 将该条目替换成第四次比赛经历，并删除 team 信息。

Construction 正确定位为：

- stage: `update_loss`
- commit: `17`
- before: `mem_conv-42_0001_v1`
- after: `mem_conv-42_0001_v2`

#### `conv-43_qa_0002`

sneaker collection 最初已被正确构建，但 commit 6 用无关的篮球生涯内容覆盖了该条目。

诊断正确定位 commit 6 的 `update_loss`。虽然 fantasy DVDs 在更晚阶段也没有被提取，但按照“只处理最早错误”的规则，不应越过 commit 6 去优先报告后面的错误。

#### `conv-44_qa_0002`

flowers 在 commit 19 没有形成候选；board games 则在 commit 23 被构建，后来才被覆盖。诊断选择更早发生的 flowers extraction omission，符合最早错误规则。

#### `conv-48_qa_0000`

原始对话明确说 Jolene 完成了 electrical engineering project，但 commit 1 没有生成候选。诊断正确定位为 `extraction`。

### 5.2 Construction 数据链仍有缺口

虽然诊断文本通常正确，但确定性溯源数据不够完整。

检查发现，相关 `memory_change_events` 中的 `before_versions` 普遍为空。比如：

- `conv-42_qa_0000` 的 commit 17 UPDATE 没有在 change event 中直接保存旧版本；
- `conv-43_qa_0002` 的 commit 6 UPDATE 同样没有保存旧版本。

模型能够从更早的 change event 和版本号推断出 before version，但这不是理想的纯算法定位。用户要求的修复包应直接包含：

```text
change_id
decision_id
direct_message_ids
before_version
after_version
changed_fields
```

目前两个 `update_loss` 报告还存在字段遗漏：

- `conv-42_qa_0000` 的 `first_error.message_ids` 为空，`decision_id` 为空；
- `conv-43_qa_0002` 的 `first_error.decision_id` 为空。

但对应 change event 实际已经存在：

- `decision_17_000`
- `decision_6_002`

这说明这些 ID 不应继续交给 LLM 自由复制，而应由程序根据 `commit_id + change_id` 确定性回填。

### 5.3 Construction 质量结论

11 个成功解析的 Construction 报告中，没有发现明显错误的主结论。它们对最早错误的判断总体可靠。

但是：

- 6/17 的 Construction 调用返回无效 JSON；
- update-loss 修复包的 before/after 数据链不够完整；
- 关键的 `decision_id` 和 `message_ids` 没有经过算法回填。

因此 Construction 的“成功样本语义质量”较高，但“全量工程可用率”只有 `11/17 = 64.7%`。

---

## 6. AnswerCheck 的质量与必要性

当前每个问题除了两个诊断 Agent，还会额外运行 `AnswerCheckAgent`：

1. 只读取最终回答上下文中的记忆；
2. 让 maintenance 模型重新回答；
3. 再由同一个 maintenance 模型判断新回答是否正确。

这个步骤没有改变 Access 或 Construction 的结论，只作为附加信息写入合并包。

在 17 个样本中：

- 1 个 AnswerCheck 返回无效 JSON；
- 多个明明已经拥有足够记忆的样本仍回答 `I don't have enough information.`；
- `conv-48_qa_0002` 中，记忆已经明确写出母亲几年前去世，它仍然拒绝回答；
- `conv-30_qa_0006` 重新回答出了 February 8, 2023，但后续判断 JSON 又解析失败。

这个步骤当前不能稳定地区分“回答模型能力问题”。原因是它测试的是 maintenance 模型，而不是原来的 runtime 模型，并且又引入了额外的一次回答和一次判断。

建议：

- 不要把 AnswerCheck 放进两个 Failure Agent 的核心完成条件；
- 如果保留，应改名为独立的 `answerability_probe`；
- 单独存储，不能污染 Access 或 Construction 包；
- 不应阻塞两个诊断 Agent 的成功状态；
- 不应由它决定是否生成 Access 或 Construction 修复案例。

---

## 7. 物理隔离不符合要求

虽然两个 Agent 的提示词上下文基本隔离：

- Access 不读取 raw conversation；
- Construction 不读取搜索链；
- 两次 LLM 调用使用独立 messages；

但物理空间和工作流仍没有隔离。

当前 `FailureWorkflow` 会：

1. 顺序调用 Access；
2. 顺序调用 Construction；
3. 调用 AnswerCheck；
4. 将三者写入同一目录；
5. 额外写一个 `*_diagnoses.json` 合并文件。

当前目录形式为：

```text
failures/<conversation-id>/
├── *_access_report.json
├── *_construction_report.json
└── *_diagnoses.json
```

这与目标结构不一致。目标应为：

```text
<run-id>/
├── judge/
├── access_failure/
│   ├── packages/
│   ├── progress.jsonl
│   └── errors.jsonl
└── cons_failure/
    ├── packages/
    ├── progress.jsonl
    └── errors.jsonl
```

当前设计带来三个实际问题：

1. **一侧失败后不能独立重试。**
   只要合并文件存在，resume 就可能跳过整个问题。
2. **整体 `completed` 掩盖局部失败。**
   17 个样本中有 7 个至少一侧是 `model_error`，但外层进度仍可记为 `completed`。
3. **后续 Skill-Maker 容易错误消费。**
   一个 Construction 有效、Access 无效的合并包可能被当成完整样本。

物理隔离不是单纯的目录美观问题，而是独立重试、独立发布和独立 Skill 修复的前提。

---

## 8. 当前运行方式的额外风险

检查时发现两套程序正在同时处理同一批问题：

1. 六个独立的 `run_judge_first_diagnosis.py` 进程；
2. 一个 `run_judge_first_diagnosis_concurrent.py` 进程，其中包含六个 conversation，每个 conversation 使用六个 worker。

理论上同时可能存在约 42 个问题级诊断任务，并且每个问题还包含多个 LLM 调用。

此外，`judge_first_diag_v2` 的输出目录在任务运行期间被移除或重建，导致大量错误：

```text
No such file or directory:
..._access_report.json.tmp
```

这不是诊断语义错误，而是输出目录在运行中消失造成的写入错误。当前串行进程仍在继续尝试后续样本，因此会持续浪费调用。

本次审查没有停止或修改任何正在运行的进程，但后续正式运行不应让两套 runner 同时处理相同数据，也不应在运行期间清理其输出目录。

并发版的另一个风险是多个线程共享同一个 maintenance client。当前大量核心 Agent 的 `model_error` 都是“返回无效 JSON”，但程序没有保存原始模型响应，因此暂时无法判断究竟是：

- 模型没有遵守 JSON；
- 并发下客户端或上游服务响应异常；
- 响应被截断；
- JSON 提取器过于脆弱。

必须先保存失败时的原始响应，才能继续精确定位。

---

## 9. 逐包评级

评级含义：

- **A**：Judge 合理，两个核心诊断完整，结论可直接用于后续修复分析；
- **B**：部分结果有价值，但存在一个诊断错误、边界 Judge 或非核心异常；
- **C**：只有一侧诊断可用，需要独立重跑另一侧；
- **D**：Judge 或两个核心诊断均不可用。

| 样本 | 评级 | 结论 |
|---|---|---|
| `conv-30_qa_0000` | C | Construction 准确定位 yesterday 日期提取错误；Access 无效 JSON |
| `conv-30_qa_0002` | B | Access 漏召回判断合理、Construction 无问题；但 Judge 是否过严存在争议 |
| `conv-30_qa_0003` | C | Access 正确指出三条必要记忆均未召回；Construction 无效 JSON |
| `conv-30_qa_0005` | A | Access 找到 Marley flooring 漏召回，Construction 找到 by-the-water 提取遗漏 |
| `conv-30_qa_0006` | B | 两个核心诊断正确；附加 AnswerCheck 无效 JSON |
| `conv-42_qa_0000` | B | Construction update-loss 正确；Access 错把不含 team 的最终记忆视为足够证据 |
| `conv-42_qa_0001` | C | Access 找到共享兴趣相关记忆漏召回；Construction 无效 JSON |
| `conv-42_qa_0002` | A | 必要记忆已召回且构建基本保留相对时间，属于回答推理失败 |
| `conv-42_qa_0003` | A | 快照无可用答案记忆，Construction 正确定位 first-tournament 日期提取错误 |
| `conv-43_qa_0000` | C | Access 找到 shooting-percentage 记忆漏召回；Construction 无效 JSON |
| `conv-43_qa_0001` | C | Access 找到 endorsements/charity 记忆漏召回；Construction 无效 JSON |
| `conv-43_qa_0002` | A | Access 正确无责，Construction 正确定位 sneaker 条目最早被覆盖的位置 |
| `conv-44_qa_0000` | C | Access 正确确认记忆已召回；Construction 无效 JSON，核心错误未完成定位 |
| `conv-44_qa_0002` | A | Access 正确无责，Construction 正确选择更早的 flowers extraction omission |
| `conv-48_qa_0000` | A | Construction 正确定位 electrical-engineering project 未被提取 |
| `conv-48_qa_0001` | D | Judge 判错合理，但 Access 与 Construction 均无效 JSON |
| `conv-48_qa_0002` | D | Prediction 与 Reference 语义等价，本不应进入诊断 |

评级统计：

| 评级 | 数量 |
|---|---:|
| A | 6 |
| B | 3 |
| C | 6 |
| D | 2 |

可以直接作为高质量研究案例的样本为：

```text
conv-30_qa_0005
conv-42_qa_0002
conv-42_qa_0003
conv-43_qa_0002
conv-44_qa_0002
conv-48_qa_0000
```

---

## 10. 修复优先级

### P0：正式批量诊断前必须完成

1. 将 Judge 接口改为真正的二元 `correct: true/false`。
2. Access 与 Construction 使用独立目录、独立进度、独立错误日志和独立 resume。
3. 不再生成合并 `*_diagnoses.json`。
4. 外层状态不能把包含 `model_error` 的 Agent 标记为成功。
5. 对失败的单个 Agent 独立重试，不重跑已成功的另一侧。
6. 保存每次无效 JSON 的原始模型响应。
7. 正式运行时只保留一套 runner，禁止相同数据被两套进程重复处理。

### P1：提高诊断包的确定性

1. 程序在模型返回后确定性回填 `decision_id`、`message_ids`、`before_version_ids` 和 `after_version_id`。
2. Construction change event 必须真正保存 parent/before versions。
3. Access 增加硬规则：只有当前版本正文能够支持答案时，才允许列入 necessary memory。
4. 若两个核心 Agent 都判定没有问题，将样本返回 Judge 复核，而不是继续生成 Skill。
5. 对语义等价的相对时间表达增加 Judge 示例，例如：
   `a few years ago` 等价于 `a few years before 2023`。

### P2：简化系统

1. 将 AnswerCheck 从核心 FailureWorkflow 中移出。
2. 如果保留 AnswerCheck，将其作为独立实验，不与两个 Failure Agent 共用完成状态。
3. 删除 `partial_aware_score` 等与诊断无关的统计字段。
4. 修复脚本、提示词和个别输出中的乱码字符，例如 `鈥檚`。

---

## 11. 最终判断

这 17 个包证明了当前架构的核心思想是成立的：

- evidence 可以通过来源 ID 反向映射到快照记忆；
- Access 可以检查这些记忆是否真正出现在自然搜索链中；
- Construction 可以沿候选、决策、版本变化定位最早错误；
- 同一个问题确实可能同时存在独立的 Access 和 Construction 问题；
- 只定位 Construction 的最早错误是可执行的。

当前最需要修复的不是诊断提示词本身，而是执行与数据契约：

1. Judge 二元化；
2. 两条诊断链物理隔离；
3. 局部失败独立重试；
4. before/after 版本链确定性补全；
5. 原始模型响应留档；
6. 避免重复 runner 和输出目录竞争。

完成这些改动后，现有成功诊断的内容质量足以继续支撑后续 Access Skill-Maker 和 Construction Skill-Maker 实验。在此之前，不建议把全部 `completed` 产物直接当作可靠训练或 Skill 生成数据。

# MiM V2 迭代实验报告

生成日期:2026-08-04

## 实验概述

单轮 V2 迭代流程:用现有 Bank1(23A+26C)在 train 6 对话(conv-30/42/43/44/48/49)完整跑一遍(6 路并行构建 + 并行答题)→ LLM Judge → V3 诊断(含 skill trace)→ 生成候选 drafts → **未用到的 skill 剔除**(access 1 个、construction 6 个,保留 22A+20C)→ 被用到的 skill 与 drafts 进行 CRUD(V2 draft-first pipeline)→ **Bank2(35A+35C)** → prune → validation 三变体评测。

主要流程脚本:`scripts/run_mim_v2_single.py`、`scripts/run_train_iter.py`、`scripts/run_candidates_from_diagnosis.py`、`scripts/prune_memory.py`。产物:`outputs/v2_iter/`。

> 注:旧四变体为各自独立构建(记忆不同),新三变体共享同一份 Bank2 构建并 prune 的记忆(受控对照)。判别实验为 26C 构建 + 35A 答题,用于分离 cons 构建影响。

## 总体结果(C 率 / C+P 率)

| # | 评测 | 记忆构建 | 答题 Skill | C | P | I | C 率 | C+P 率 |
|---|------|----------|------------|----|----|----|-----|------|
| 1 | Bank0 基线 | 无 Skill(独立构建) | 无 | 167 | 65 | 160 | 42.6% | 59.2% |
| 2 | 旧 Access-only | 无 Skill(独立构建) | 23A | 177 | 54 | 161 | 45.2% | 58.9% |
| 3 | 旧 Cons-only | 26C(独立构建) | 无 | 183 | 60 | 149 | 46.7% | 62.0% |
| 4 | 旧 Full | 26C(独立构建) | 23A | 174 | 68 | 150 | 44.4% | 61.7% |
| 5 | 新 Cons-only | Bank2-35C(共享) | 无 | 155 | 71 | 166 | 39.5% | 57.7% |
| 6 | 新 Access-only | Bank2-35C(共享) | 35A | 159 | 69 | 164 | 40.6% | 58.2% |
| 7 | 新 Full | Bank2-35C(共享) | 35A | 161 | 63 | 168 | 41.1% | 57.1% |
| 8 | 判别(26C构建+35A答题) | 旧 26C(独立构建) | 35A | 169 | 64 | 159 | 43.1% | 59.4% |

## 分题型 C 率

| 题型 | 题数 | bank0 | 旧acc | 旧cons | 旧full | 新cons | 新acc | 新full | 判别 |
|---|---|---|---|---|---|---|---|---|---|
| Single-hop | 63 | 31.7% | 36.5% | 30.2% | 25.4% | 25.4% | 25.4% | 25.4% | 34.9% |
| Temporal | 64 | 35.9% | 42.2% | 54.7% | 48.4% | 39.1% | 40.6% | 39.1% | 42.2% |
| Open-domain | 21 | 38.1% | 28.6% | 23.8% | 19.0% | 23.8% | 28.6% | 28.6% | 33.3% |
| Multi-hop | 156 | 53.8% | 52.6% | 53.8% | 53.2% | 48.7% | 49.4% | 52.6% | 51.3% |
| Adversarial | 88 | 36.4% | 44.3% | 45.5% | 45.5% | 37.5% | 38.6% | 36.4% | 37.5% |

## 分题型 C+P 率

| 题型 | 题数 | bank0 | 旧acc | 旧cons | 旧full | 新cons | 新acc | 新full | 判别 |
|---|---|---|---|---|---|---|---|---|---|
| Single-hop | 63 | 81.0% | 77.8% | 74.6% | 74.6% | 77.8% | 73.0% | 71.4% | 82.5% |
| Temporal | 64 | 43.8% | 46.9% | 59.4% | 54.7% | 43.8% | 46.9% | 46.9% | 48.4% |
| Open-domain | 21 | 47.6% | 42.9% | 38.1% | 33.3% | 33.3% | 47.6% | 42.9% | 52.4% |
| Multi-hop | 156 | 71.2% | 66.7% | 70.5% | 72.4% | 69.9% | 69.2% | 69.2% | 67.9% |
| Adversarial | 88 | 36.4% | 44.3% | 45.5% | 45.5% | 37.5% | 38.6% | 36.4% | 37.5% |

## Skill Bank 构成

| Bank | Access | Construction | 来源 |
|---|---|---|---|
| Bank1(起点) | 23 | 26 | exp/single-agent/bank1_draft_crud_v2/banks |
| Bank1 过滤后(参与 CRUD) | 22 | 20 | 剔除 train 未使用 skill(access 1、cons 6) |
| Bank2(发布) | 35 | 35 | 过滤后 skill + 13A/15C 新 drafts |
| 新 drafts 来源 | 256 候选 → 82 草稿 | | train 诊断 repair packages(715 道 P/I 题) |

剔除的 access skill:`sk_access_temporal_evidence_verification`

剔除的 construction skills:`sk_construction_draft_f134a3428b`、`a8a191568c`、`494786eaa0`、`37cb0fc49e`、`80515dc5fb`、`7df961c640`

## 关键分析

### 1. Access Skill 毒害:新集合基本消除(同记忆干净对照)

新 cons → 新 full 过渡矩阵(同一份记忆):

- cons C → full I: 14
- cons C → full P: 5
- cons I → full C: 15
- cons I → full P: 6
- cons P → full C: 10
- cons P → full I: 9

净效应 ≈ **+3**(毒害 28 道 vs 修复 31 道)。对比旧实验(独立构建,39 毒害 / 38 修复),本轮干净对照下 access 注入几乎无净伤害。残留毒害集中在 **Single-hop(-6.4pp)**;Temporal 上新 full 反而高于新 cons(+3.1pp)。

### 2. 新 Cons Drafts 造成构建退化(判别实验坐实)

| 对照 | C+P | 差异 |
|---|---|---|
| 判别(26C 构建 + 35A 答题) | 233 | — |
| 新 full(35C 构建 + 35A 答题) | 224 | 26C 构建高 **+9** |
| 新 acc(35C 构建 + 35A 答题) | 228 | 26C 构建高 **+5** |

同样 35A 答题下,26C 构建显著优于 35C 构建 → **新加入的 15 个 cons drafts 污染了记忆构建**。退化重灾区:Temporal(-15.6pp,59.4→43.8)与 Adversarial(-7~9pp);而 Single-hop 上新 cons 构建反而改善(+3.2pp)。高频新构建 skill:`f2f06eb1e7`(Extract explicit motivation drivers,17+8 次触发),属无条件提取型指令。

### 3. 新 Access Drafts 的收益

- **Open-domain**:新 full 42.9% vs 旧 full 33.3%(**+9.6pp**),新 acc 47.6% vs 旧 42.9%(+4.7pp)
- **Multi-hop**:持平(69~72%)
- 13 个新 access drafts 主要修复开放域/间接证据类问题

### 4. 其他

- **Memory pruning**:train prune 后未影响评测(val 记忆全部被引用,删 0 条)
- **Skill content 限制**:新 Bank2 所有 skill content ≤ 6 条(旧 Bank1 有 10 条/13 条的超限 skill,系限制加入前发布)
- **skill 池膨胀**:42 → 70(+67%),`min_candidate_support=1` 门槛放水,是候选数量膨胀的直接原因

## 结论

1. **迭代机制有效**:未用 skill 剔除 + 剩余与 drafts CRUD 后,access 毒害在干净对照下消失,Open-domain 获得显著修复
2. **新 cons drafts 是本次净损失的主因**(-17~18):为 train 失败模式定制的指令泛化到 val 后污染时间/对抗性信息提取
3. **下一步建议**:① 提高 `min_candidate_support` 限制每轮新增 skill 数量;② 只采纳在 val 上有收益的 cons drafts;③ 将无条件提取型指令改为条件触发;④ 用 26C 构建 + 35A 答题组合作为短期回归基线

## 根因深入分析(基于记忆库逐条对比)

判别实验保留了两份 val 记忆库(26C 构建 / 35C 构建),以下分析直接对比它们的记忆内容和 skill 注入轨迹。

### 三个假设的验证

**假设 1:skill 不对口 —— 成立,分两层**

- **构建注入层**:35C 构建中时间解析 skill `494786eaa0`(Resolve relative event times)**0 次注入**(26C 构建 1 次),`f705d2a7d0`(自述偏好分离)从 4 次降至 3 次 —— 新 skill 抢占了每 session `top_k=2` 的注入名额(f2f06eb1e7 8 次、6e6a7a4dac 3 次、798576272b 2 次,共 13 次注入在 26C 构建中本属于旧 skill)
- **记忆检索层**:35C 构建中动机/支持类记忆(如 05-08 的 LGBTQ support group,由 f2f06eb1e7 创建)与关键事件记忆(07-10 的 LGBTQ conference)语义高度重叠,基础检索排错序,qa_0025 答成 05-08(26C 构建下正确检索到 07-10)
- 注意:答题侧 35A 在判别实验(26C 记忆)上 Single-hop 82.5% 为全部版本最高 → **35A 本身对口,不对口发生在构建层**

**假设 2:skill 范围太宽需拆分 —— 成立,证据最直接**

同一条源信息,两个 skill 提取出完全不同的记忆:

| 构建 | 创建 skill | 提取出的记忆 | 信息保真 |
|---|---|---|---|
| 26C | `f705d2a7d0`(自述偏好,窄触发) | "...have been with her since she moved... **four years ago**" | 保留时长 ✓ |
| 35C | `f2f06eb1e7`(动机,宽触发) | "...who provide her with love, guidance, and acceptance" | **丢失时长**,泛化为动机 ✗ |

`f2f06eb1e7` 触发描述"When a message states what motivates, drives, or keeps a person going"覆盖全部动机场景 → 19 个 session 触发 8 次,创建 **37 条**动机记忆(全库第二多),每条信息密度低、事实降维(日期/时长/参与者被提炼掉)。对比旧 skill `f7c0b40981`(他人点名爱好)、`f705d2a7d0`(自述偏好)触发窄、提取保真。

**假设 3:skill 过拟合 —— 成立(来源层面)**

- `f2f06eb1e7` 完全来自 train 诊断(train 6 对话的动机类失败),在 val(conv-26/41)上过度应用
- 256 个候选在 `min_candidate_support=1` 下全部放行 → 82 drafts → 70 skills,过拟合 draft 无门槛进入官方 bank
- 判别实验定量:同样 35A 答题,35C 构建(224/228)vs 26C 构建(233),新 drafts 净损 5~9 分

### 补充发现:构建方差

两次"26C 构建"之间(旧 cons 243 vs 判别 233)差 **10 分**,大于 35C/26C 的 skill 差异(5~9 分)。单次构建噪声与 skill 效应同量级,但跨 3 个变体一致的退化模式(Temporal -15.6pp、Adversarial -7~9pp)+ 逐条记忆证据,使"新 drafts 有害"结论依然成立。

### 完整因果链

```
train 失败 → 诊断 → 256 候选(min_support=1 全放行)
  → 宽触发 drafts 进 bank(f2f06eb1e7 等)
  → 构建时抢占 top_k=2 注入名额(假设1:对口 skill 出局,时间解析 0 次注入)
  → 宽范围 skill 提取时事实降维(假设2:日期/时长被提炼成动机)
  → 泛化动机记忆成为检索干扰项(假设1:相似事件排序错乱)
  → Temporal/Adversarial 全面退化
```

### 修复方向(对应三个假设)

1. **拆分/收窄触发(治假设 2)**:`f2f06eb1e7` 类 skill 必须附"保留原始事实细节(日期/时长/参与者/数字),动机仅作附加属性";或按主题拆成多个窄 skill
2. **注入配额保护(治假设 1)**:关键保真 skill(时间解析、偏好分离、事件细节)在构建注入时保底名额;skill 注入按"事件细节类/偏好类/动机类"互斥分组
3. **门槛与验证(治假设 3)**:`min_candidate_support` 提到 2~3;新增 drafts 在 val 子集上验证收益后才发布;每轮新增 skill 设上限(如 ≤10/侧)

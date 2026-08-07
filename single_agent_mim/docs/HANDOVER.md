# MiM(Memory in Memory)项目交接文档

> 生成日期:2026-08-07 | 交接范围:项目全貌、文件结构、核心思路、评测结果、已知问题

---

## 1. 项目概述

MiM 是一个**错误驱动的元记忆层(memory layer)** 系统:在下游 LLM 长对话记忆问答出错时,系统自动诊断错误来源(记忆构建侧 vs 记忆访问侧),把修复经验抽象为**可复用的自然语言 Skill**,注入运行时(记忆构建 + 答题)以改进后续行为。

- **评测基准**:LoCoMo 长对话记忆问答
- **运行时模型(runtime)**:qwen3-8b(默认基座),也可用 deepseek-v4-flash
- **维护模型(maintenance)**:deepseek-v4-flash(诊断、skill 生成、评测 judge)
- **评测判官**:LLM-as-judge,CORRECT/WRONG 二分类(community-standard 宽松语义判断)

### 核心流程(一条完整的迭代闭环)

```
训练集 6 对话构建+答题 (runtime + Skill Bank)
  → LLM Judge (binary C/W)
  → Diagnosis V3 三阶段 (answer → access → cons, 带 skill trace 溯源)
  → 候选 Skill 生成 (candidates, 保守提示词 + 成功案例校准)
  → K-means 聚类 → 1-5 个草稿/簇 (drafts)
  → CRUD 合并进 Skill Bank (add/merge/update/delete)
  → 验证集/测试集构建+答题 → Judge
  → 回归分析 → 下一轮迭代
```

---

## 2. 文件结构

```
D:/Documents/Project/Memory_in_Memory/
├── LoCoMo/                    # 评测数据集 (locomo10.json)
├── exp/single-agent/          # ★ 定档 Skill Bank 与评测结果
│   ├── bank_empty/            #   空 bank(交换实验起点)
│   ├── bank_v0/               #   无 skill 基线
│   ├── bank_v1/               #   23A+26C(提示版,旧定档)
│   └── bank_v2/               #   32A+49C(保守生成,历史最优档)
│       ├── banks/             #     skill 文件(access/construction)
│       └── validation/latest/ #     评测数据(conv-26/41 + judge)
└── single_agent_mim/          # ★ 主工程
    ├── configs/
    │   ├── qwen3_8b_dashscope.yaml   # 默认配置(runtime=qwen3-8b)
    │   ├── deepseek_runtime.yaml     # 实验配置(runtime=deepseek)
    │   └── qwen3_8b_swap.yaml        # 交换实验配置(自定义 split)
    ├── src/mim/
    │   ├── agents/            #   核心 agent
    │   │   ├── access.py      #     访问+答题(含 skill 注入,JSON 修复重试)
    │   │   ├── construction.py#     记忆构建(提取+CRUD 决策)
    │   │   ├── access_failure.py / answer_failure.py / cons_failure.py
    │   │   │                   #     三阶段诊断 agent
    │   │   └── skill_learning.py  # 候选 skill 生成(含成功案例)
    │   ├── diagnosis/         #   诊断工作流(runner/workflows/evidence)
    │   ├── skill_maker/       #   skill 校验/聚类/存储
    │   ├── skills.py          #   SkillBank 加载/召回/reranker
    │   ├── llm/               #   模型客户端(factory/round_robin)
    │   ├── retrieval/         #   记忆检索(语义/BM25/混合)
    │   ├── storage/           #   SQLite 存储
    │   ├── eval/              #   LoCoMo 数据集加载/指标
    │   └── tracing.py         #   并行写锁
    ├── scripts/               # ★ 运行入口
    │   ├── run_train_iter.py        # 并行训练/验证(构建+答题)
    │   ├── run_mim_loose_iter.py    # 完整迭代编排(可指定起始 bank)
    │   ├── judge_binary.py          # binary C/W judge(确定性)
    │   ├── judge_predictions.py     # C/P/I judge(旧)
    │   ├── judge_ratings.py         # 1-5 评分 judge(旧)
    │   ├── run_answer_failure.py / run_access_failure.py / run_cons_failure.py
    │   ├── run_candidates_from_diagnosis.py  # 候选生成
    │   ├── run_skill_bank_pipeline_v2.py     # 聚类+草稿+CRUD
    │   ├── build_successful_skill_traces.py  # 成功案例构建
    │   └── prune_memory.py       # 记忆裁剪
    ├── prompts/               # ★ 所有提示词
    │   ├── access.md / construction_extraction.md / construction_decision.md
    │   ├── diagnosis/         #   三阶段诊断提示词
    │   ├── judge/             #   judge 提示词(binary/rating)
    │   ├── skill_maker/       #   候选生成/CRUD/聚类提示词
    │   └── backup_*/          # ★ 历史版本备份(勿删)
    ├── data/splits/           #   数据集划分
    │   ├── locomo_6_2_2.json        # 默认(6 train/2 val/2 test)
    │   └── locomo_swap_4_2_2.json   # 交换实验(conv-47/50 入 train)
    ├── outputs/               # 实验产物(诊断包/候选/bank/评测)
    └── docs/                  # 文档(FINAL_RESULT/本交接文档等)
```

---

## 3. 核心思路

### 3.1 错误驱动学习

不预先定义"好记忆",而是**从错误中学习**:
1. 在训练集上运行 → 判官给出错误答案 → 三阶段诊断定位错误根源
2. 诊断产出"修复包"(problem_found + repair_package),含 skill trace 溯源
3. 修复包 → 候选 skill → 聚类 → 草稿 → CRUD 进 Skill Bank

### 3.2 三阶段诊断(Diagnosis V3)

| 阶段 | 问的问题 | 输入 |
|---|---|---|
| answer | 答案能否从已检索上下文直接得出? | 检索链 |
| access | 访问侧是否检索到足够证据? | 当前快照记忆 |
| cons | 构建侧是否提取/保留了该事实? | 构建历史+版本链 |

每阶段带 **skill_trace**(该题召回了哪些 skill、选中哪些),用于溯源。

### 3.3 Skill 注入哲学

**"advisory references, not commands"**(参考策略,非命令):
- 简单直接查询走默认策略,skill 不强制
- 复杂题可参考 skill 扩展检索
- **证据优先于 skill 指令**

### 3.4 保守性约束(重要迭代教训)

候选生成和 CRUD 提示词包含强制约束:
- 每条 skill 内容必须有**不适用边界**(何时不用)
- 禁止"推断缺失事实/幻觉"类指令
- 触发条件宁窄勿宽

### 3.5 确定性评测

- judge 关闭 thinking(temperature=0 才生效 —— DeepSeek V4 在 thinking 开启时**静默忽略 temperature**)
- 同一答案 judge 两次仍有 5.4% 翻盘(旧配置),确定性 judge 后消除

---

## 4. 评测结果汇总

> 判官:deepseek-v4-flash binary C/W(确定性配置);runtime 默认 qwen3-8b;validation=conv-26/41,test=conv-47/50

### 4.1 原始实验(标准 split:train=conv-30/42/43/44/48/49)

| 版本 | Skill Bank | validation | test | train(自身) |
|---|---|---|---|---|
| baseline | 无 skill | 50.0% | 51.3% | — |
| v1(提示版) | 23A+26C | 53.8% | — | — |
| **v2(历史最优档)** | **32A+49C** | **57.4%** | 49.0% | — |
| v3(激进) | 40A+53C | 53.3% | — | — |
| v3b(成功案例) | 53A+76C | 53.6% | — | — |

- **v2 在 validation 上 +7.4pp**,但 test 上 -2.3pp —— 原始 test(conv-47/50)与 train 分布差异大
- v3/v3b 的 skill 膨胀(93/129)效果下降

### 4.2 交换实验(swap split:train 含 conv-47/50,test=conv-48/49)

| 版本 | Skill Bank | validation | test | train(自身) |
|---|---|---|---|---|
| baseline | 无 skill | 53.3% | 51.3% | — |
| **v1_b(交换最优)** | **27A+91C** | **57.4%** | **52.9%** | **51.3%** |
| v1_c(规模约束) | 29A+58C | 52.0% | 52.0% | — |
| v2_b(迭代) | 43A+128C | 45.5% | 56.8% | 51.3% |

### 4.3 各版本增益全景(相对各自 baseline)

| 版本 | 增益来源 | train | validation | test |
|---|---|---|---|---|
| v1_b | 空 bank 纯诊断生成 | +2.8pp | +4.1pp | +1.6pp |
| v1_c | +规模约束 | — | -1.3pp | +0.7pp |
| v2_b | v1_b 迭代 | +2.8pp | **-7.8pp** | +5.5pp |
| v2(原) | bank_v1 起点 | — | +7.4pp | -2.3pp |

### 4.4 DeepSeek runtime 实验(qwen → deepseek-v4-flash)

| 数据集 | 配置 | qwen3-8b | deepseek-v4-flash |
|---|---|---|---|
| validation | v2 | 57.4% | 58.2% |
| validation | base | 50.0% | 54.8% |
| test | v2 | 49.0% | 54.6% |
| test | base | 51.3% | 57.1% |

- deepseek 全面 +0.8~+5.8pp(模型更强)
- 但 skill 增益结构不变(validation 正、test 负)—— 不是"qwen 遵循 skill 能力不足"

### 4.5 各题型表现(validation,确定性 judge)

| 题型 | v1_b | v2 | baseline |
|---|---|---|---|
| Multi-hop | 54.0% | 52.4% | 47.6% |
| Temporal | 51.6% | 50.0% | 37.5% |
| Open-domain | 47.6% | 47.6% | 28.6% |
| Single-hop | 69.2% | 63.5% | 64.1% |
| Adversarial | 45.5% | 42.0% | 40.9% |

---

## 5. 关键发现与教训

### 5.1 评测方法学(最重要)

1. **judge 随机性是最大噪声源**:thinking 开启时 temperature=0 被静默忽略(DeepSeek 文档),同一答案 judge 两次 5.4% 翻盘(±1.3pp)。**必须关 thinking 才确定性**
2. **构建不完整会制造假回归**:网络错误导致 session 缺失(如 conv-41 缺 s29/s30)→ 记忆缺失 → 分数暴跌。**对比前必须校验 commits 完整性**
3. **resume 补答会产生重复行**:locomo_predictions.jsonl 会累积重复 → judge total 虚高 → C 率稀释。**judge 前必须去重**
4. **单次运行不可信**:构建方差 ±2-3pp,judge 波动 ±1.3pp。跨批对比尤其危险

### 5.2 Skill 规模与效果(倒 U 型,后证伪)

- 早期观察"81 最优、129 下降"被 v1_b(118)+4.1pp 推翻 —— **skill 数量不是决定因素**
- v1_c 的规模约束(强制合并/禁新增)反而**丢掉有效机制** → -5.4pp
- v2_b 的 171 个 skill 在 conv-26(全新人物)上 -15.9pp —— **真正有害的是"错误的 skill 在错误场景被触发"**

### 5.3 泛化问题:不是过拟合,是分布耦合

- 交叉验证(交换 split):同一流程在 test 上从 -2.3 翻转到 +1.6 —— **流程有效,是 train/test 对话分布差异**
- skill 触发基于**话语模式**(句式),不同主题下语义漂移
- 与 train 主题越近的对话,增益越大(conv-49 绘画 +9.2pp vs conv-48 自然 -4.6pp)

### 5.4 构建侧 skill 的双刃剑

- v2 构建侧 49 个 skill 改变记忆提取数量:validation 多提取(+13 候选)→ 更好;test 少提取(-15)→ 更差
- 同一 skill(如 8536bfb078 第一人称归属)在 conv-41 触发 23 次有效、conv-47 触发 24 次有害

### 5.5 其他教训

- 空 bank 起点:CRUD 无"合并到已有"机会 → 全部 ADD → 91C 膨胀(但无害,见 5.2)
- 严格成功案例需要 `evidence_ids` 字段兼容(曾因此 construction 例为 0)
- 规模约束的"合并"操作会丢细粒度机制,伤害大于收益

---

## 6. 当前最优与未解问题

### 当前最优

| 场景 | 最优 | 说明 |
|---|---|---|
| 原始 split | v2(32A+49C) | validation 57.4%,test 49.0% |
| 交换 split | v1_b(27A+91C) | 三集全正(51.3/57.4/52.9%) |

### 未解问题

1. **原始 test(conv-47/50)的负增益**:skill 无法泛化到分布差异大的对话
2. **v2_b 迭代失败**:为什么新增 skill 在 conv-26 上系统性有害?(疑似 reranker 池变大 → 错误选中,未完全验证)
3. **skill 触发与主题耦合**:如何让 skill 学到"跨主题一致的语义模式"而非"句式模式"?
4. **构建侧 skill 的记忆量调节**:同一 skill 在不同对话上多提取/少提取,机制未明
5. **成功案例的价值**:严格版(870)是否真提升候选质量,未做消融

---

## 7. 复现命令速查

```bash
cd single_agent_mim

# 1. 训练集构建+答题
python scripts/run_train_iter.py --config configs/qwen3_8b_dashscope.yaml \
    --split train --run-root outputs/<run> --skill-bank-dir <bank> --build-workers 6 --qa-workers 10

# 2. binary judge(确定性)
python scripts/judge_binary.py --config configs/qwen3_8b_dashscope.yaml \
    --workers 6 --output-dir <run>/judge_binary <run>/conv-*/locomo_predictions.jsonl

# 3. 诊断三阶段
python scripts/run_answer_failure.py --config ... --judge-results <run>/judge_binary/judgments.jsonl \
    --diagnosis-run-id X --output-root <run>/diagnosis --workers 4 --source-run conv-XX=<run>/conv-XX ...

# 4. 成功案例(注意 evidence_ids 兼容已修复)
python scripts/build_successful_skill_traces.py --runtime-root <run> \
    --judgments <run>/judge_binary/judgments.jsonl --output <run>/success_examples.jsonl

# 5. 候选生成
python scripts/run_candidates_from_diagnosis.py --config ... --diagnosis-root <run>/diagnosis \
    --skills-dir <run>/skills --success-examples <run>/success_examples.jsonl --workers 8

# 6. CRUD → 新 bank
python scripts/run_skill_bank_pipeline_v2.py --config ... --source-candidates <run>/skills/candidates \
    --run-id bankX --output-root <run> --initial-skill-bank-dir <起始bank>

# 7. 完整迭代编排(1→6 自动)
python scripts/run_mim_loose_iter.py --config ... --output-root <run> --initial-bank <起始bank>
```

### 关键校验(每次对比前必做)

1. **commits 完整性**:`SELECT count(*) FROM construction_commits` 应与 session 数一致
2. **judge 去重**:locomo_predictions.jsonl 按 qa_id 去重后再 judge
3. **错误清零**:qa_results.jsonl 无 error 行
4. **同批对比**:对比对象必须同环境、同批次构建

---

## 8. 环境配置

- **runtime**:qwen3-8b(DashScope)或 deepseek-v4-flash(3 key 轮换)
- **maintenance**:deepseek-v4-flash,3 key(round-robin),**thinking 关闭**(configs/qwen3_8b_dashscope.yaml 已改)
- **embedding**:sentence-transformers/all-MiniLM-L6-v2(CPU)
- **存储**:SQLite + WAL
- **网络**:依赖外网 API,断网时全部流程阻塞(曾断 12+ 小时)
